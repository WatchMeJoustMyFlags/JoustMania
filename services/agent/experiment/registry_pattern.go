package experiment

import (
	"context"
	"fmt"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
)

// registry_pattern.go: pattern-driven shadow games in the experiment registry
// (UC-046, #1258). An experiment pins its archetypes at its first spawn
// (pinPending), runs one pair per archetype (nextPair), one pattern game at a time
// (BR-010), concludes when every archetype is used (A6), and journals the pin, each
// game's archetype and each game's replay record.

// PatternPinner resolves the experiment.mock_archetypes selection in an
// experiment's first allocation pass (UC-046 step 2). Production reads the live
// agent flags and always wires one (experiments are archetype-only); nil is a
// test-only seam for the allocation mechanics.
type PatternPinner interface {
	Pin(ctx context.Context) patterngame.Pin
}

// PatternSpawn is what a pattern arm's Spawn needs; it rides on the spawn ctx so
// the ShadowSpawner seam keeps its signature.
type PatternSpawn struct {
	Archetype      patterngame.Archetype
	MaxGameSeconds int
	PairIndex      int
	// FlagDigest is UC-046 BR-011's resolved-values digest for this arm, computed
	// before the spawn so game_assigned can journal it.
	FlagDigest string
}

type patternSpawnKey struct{}

// WithPatternSpawn attaches a pattern arm to a spawn ctx.
func WithPatternSpawn(ctx context.Context, p PatternSpawn) context.Context {
	return context.WithValue(ctx, patternSpawnKey{}, p)
}

// PatternSpawnFrom returns the pattern arm of a spawn ctx, if any.
func PatternSpawnFrom(ctx context.Context) (PatternSpawn, bool) {
	p, ok := ctx.Value(patternSpawnKey{}).(PatternSpawn)
	return p, ok
}

// pinPending pins every live experiment that has not resolved its selection yet.
// The flags are read OUTSIDE the registry lock (flagd I/O). An invalid or too-small
// selection discards the experiment (UC-046 A2, A4, A13).
func (r *Registry) pinPending(ctx context.Context) {
	if r.patterns == nil {
		return
	}
	r.mu.Lock()
	var pending []string
	pendingKey := map[string]string{}
	for id, e := range r.entries {
		if e.status == StatusRunning && e.pin == nil {
			pending = append(pending, id)
			pendingKey[id] = e.flagKey
		}
	}
	r.mu.Unlock()

	for _, id := range pending {
		// UC-043 A17 step 2: a hold-flag experiment needs the current defaults first; a
		// failed read leaves it unpinned until the next tick.
		holdReason, err := r.checkHold(ctx, pendingKey[id])
		if err != nil {
			r.log.Debug("experiment.hold_gate_unreadable", "experiment_id", id, "error", err)
			continue
		}
		pin := r.patterns.Pin(ctx)
		if IsHoldKey(pendingKey[id]) && pin.DiscardReason == "" {
			// UC-046 A17: every pinned archetype must press the trigger (BR-018).
			if missing := ArchetypesWithoutTriggerPolicy(pin); len(missing) > 0 {
				pin.DiscardReason = patterngame.DiscardTriggerPolicyMissing
				pin.Errors = []string{"archetypes without trigger_policy: " + strings.Join(missing, ", ")}
			} else if holdReason != "" {
				pin.DiscardReason = holdReason
				pin.Errors = []string{"shield.activation does not resolve as the experiment needs (UC-043 A17)"}
			}
		}
		r.mu.Lock()
		e := r.entries[id]
		if e == nil || e.status != StatusRunning || e.pin != nil {
			r.mu.Unlock()
			continue
		}
		p := pin
		e.pin = &p
		flagKey := e.flagKey
		if pin.Active() {
			r.appendEvent(e.journal, journal.Event{
				Kind: journal.KindPatternPinned,
				Note: pin.CapNote,
				Pattern: &journal.PatternFacts{
					Selection:      pin.Selection,
					Archetypes:     pin.Names(),
					ParamsDigests:  pin.Digests(),
					ParamsJSON:     pin.ParamsJSON(),
					MaxGameSeconds: pin.MaxGameSeconds,
				},
			})
			if pin.CapNote != "" {
				r.log.Warn("experiment.pattern_cap_fallback", "experiment_id", id, "note", pin.CapNote)
			}
			if e.span != nil {
				e.span.AddEvent("experiment.pattern_pinned", trace.WithAttributes(
					attribute.String("pattern.selection", pin.Selection),
					attribute.StringSlice("pattern.archetypes", pin.Names()),
					attribute.StringSlice("pattern.params_digests", pin.Digests()),
					attribute.Int("pattern.max_game_seconds", pin.MaxGameSeconds),
				))
			}
			r.log.Info("experiment.pattern_pinned", "experiment_id", id,
				"archetypes", pin.Names(), "max_game_seconds", pin.MaxGameSeconds)
		}
		r.mu.Unlock()

		if pin.DiscardReason != "" {
			reason := pin.DiscardReason + ": " + strings.Join(pin.Errors, "; ")
			r.log.Warn("experiment.pattern_discarded", "experiment_id", id, "reason", reason)
			r.teardown(ctx, id, flagKey, StatusDiscarded, reason)
		}
	}
}

// checkHold runs the hold gate for a hold-flag experiment ("" for any other flag or
// with no gate wired).
func (r *Registry) checkHold(ctx context.Context, flagKey string) (string, error) {
	if r.holdGate == nil || !IsHoldKey(flagKey) {
		return "", nil
	}
	return r.holdGate(ctx, flagKey)
}

// discardHoldStale tears down a running, pinned hold-flag experiment whose condition
// no longer holds (UC-043 A17 step 2): shield.activation left hold under a
// sub-parameter experiment, or reached hold under a shield.activation experiment. A
// read error keeps the experiment (a transient error is no evidence).
func (r *Registry) discardHoldStale(ctx context.Context) {
	if r.holdGate == nil {
		return
	}
	r.mu.Lock()
	running := map[string]string{}
	for id, e := range r.entries {
		if e.status == StatusRunning && e.pin != nil && IsHoldKey(e.flagKey) {
			running[id] = e.flagKey
		}
	}
	r.mu.Unlock()
	for id, flagKey := range running {
		reason, err := r.holdGate(ctx, flagKey)
		if err != nil || reason == "" {
			continue
		}
		r.log.Warn("experiment.hold_discarded", "experiment_id", id, "flag", flagKey, "reason", reason)
		r.teardown(ctx, id, flagKey, StatusDiscarded, reason)
	}
}

// patternActive reports whether the entry runs pattern games. Assumes r.mu is held.
func (e *entry) patternActive() bool { return e.pin != nil && e.pin.Active() }

// patternInFlightLocked reports whether any pattern game is in flight (BR-010).
func (r *Registry) patternInFlightLocked() bool {
	for _, e := range r.entries {
		if e.patternActive() && e.inFlight() > 0 {
			return true
		}
	}
	return false
}

// patternExhaustedLocked reports whether a pattern experiment has spawned one pair
// per pinned archetype (UC-046 A6). Assumes r.mu is held.
func (e *entry) patternExhaustedLocked() bool {
	return e.patternActive() && len(e.arms) > 0 && e.armCursor >= len(e.pin.Archetypes)*len(e.arms)
}

// concludeIfPatternExhausted concludes a pattern experiment whose archetypes are
// all used and whose last game has ended, with the verdict of the pairs it has
// (UC-046 A6). Returns whether it concluded.
func (r *Registry) concludeIfPatternExhausted(ctx context.Context, id string) (Status, bool) {
	r.mu.Lock()
	e := r.entries[id]
	if e == nil || e.status != StatusRunning || !e.patternExhaustedLocked() || e.inFlight() > 0 {
		r.mu.Unlock()
		return "", false
	}
	jrnl := e.journal
	r.mu.Unlock()
	final := journal.Verdict{Outcome: OutcomeInconclusive, Reason: "pattern archetypes exhausted (UC-046 A6)"}
	if r.verdict != nil {
		if v, ok := r.verdict.Evaluate(jrnl.Summary()); ok {
			final = v
			if final.Reason == "" {
				final.Reason = "pattern archetypes exhausted (UC-046 A6)"
			} else {
				final.Reason += "; pattern archetypes exhausted (UC-046 A6)"
			}
		}
	}
	st, _ := r.conclude(ctx, id, final)
	return st, true
}

// patternFactsLocked builds the game_assigned pattern facts of a pair. Assumes r.mu is held.
func (e *entry) patternFactsLocked(pairIndex int, spawn *PatternSpawn) *journal.PatternFacts {
	if !e.patternActive() || pairIndex < 0 || pairIndex >= len(e.pin.Archetypes) {
		return nil
	}
	a := e.pin.Archetypes[pairIndex]
	facts := &journal.PatternFacts{
		Archetype:    a.Name,
		ParamsDigest: a.Digest,
		RngSeed:      pairSeed(e.id, pairIndex),
	}
	if spawn != nil {
		facts.FlagDigest = spawn.FlagDigest
	}
	return facts
}

// RecordPatternStart records a started pattern game of an experiment: the
// experiment.pattern_game event on the experiment span (telemetry.md §9). Wired to
// the runner's PatternStartCallback. Unknown experiments are ignored.
func (r *Registry) RecordPatternStart(info patterngame.Info) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[info.ExperimentID]
	if e == nil || e.span == nil {
		return
	}
	e.span.AddEvent("experiment.pattern_game", trace.WithAttributes(
		attribute.String("game.id", info.GameID),
		attribute.String("experiment.arm", info.Arm),
		attribute.Int("experiment.pair_index", info.PairIndex),
		attribute.String("pattern.archetype", info.Archetype.Name),
		attribute.String("pattern.params_digest", info.Archetype.Digest),
		attribute.String("game.rng_seed", fmt.Sprintf("%d", info.Seed)),
		attribute.Int("pattern.stream_rate_hz", info.Archetype.StreamRateHz),
		attribute.StringSlice("pattern.slot_serials", info.Serials),
		attribute.Int("pattern.max_game_seconds", info.MaxGameSeconds),
		attribute.String("pattern.flag_digest", info.FlagDigest),
	))
}

// RecordPatternEnd journals a finished pattern game's replay record
// (pattern_game_ended) and adds experiment.pattern_game_ended to the experiment
// span (UC-046 step 8). The fitness sample is NOT folded here: it arrives on the
// telemetry path (ConcludeGame) like every game's. Unknown experiments are ignored.
func (r *Registry) RecordPatternEnd(rec patterngame.Record) {
	winner := rec.WinnerSlot
	exact := rec.Exact()
	censored := rec.Censored
	facts := &journal.PatternFacts{
		Censored:         &censored,
		OrderSource:      rec.OrderSource,
		Archetype:        rec.Archetype.Name,
		ParamsDigest:     rec.Archetype.Digest,
		RngSeed:          rec.Seed,
		FlagDigest:       rec.FlagDigest,
		Outcome:          rec.Outcome,
		EliminationOrder: rec.EliminationOrder,
		WinnerSlot:       &winner,
		SignalHashes:     rec.SignalHashes(),
		Checkpoints:      rec.Checkpoints(),
		ReplayExact:      &exact,
		NonExactReasons:  rec.NonExactReasons,
		Decisions:        rec.DecisionSequence(),
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[rec.ExperimentID]
	if e == nil {
		return
	}
	e.countPatternOutcomeLocked(rec.Arm, rec.Outcome)
	pi := rec.PairIndex
	r.appendEvent(e.journal, journal.Event{
		Kind:      journal.KindPatternGameEnded,
		GameID:    rec.GameID,
		Arm:       rec.Arm,
		PairIndex: &pi,
		Pattern:   facts,
	})
	if e.span != nil {
		e.span.AddEvent("experiment.pattern_game_ended", trace.WithAttributes(
			attribute.String("game.id", rec.GameID),
			attribute.String("pattern.archetype", rec.Archetype.Name),
			attribute.String("pattern.params_digest", rec.Archetype.Digest),
			attribute.String("pattern.outcome", rec.Outcome),
			attribute.Bool("pattern.censored", rec.Censored),
			attribute.String("pattern.order_source", rec.OrderSource),
			attribute.IntSlice("pattern.elimination_order", rec.EliminationOrder),
			attribute.Int("pattern.winner_slot", rec.WinnerSlot),
			attribute.StringSlice("pattern.signal_hashes", rec.SignalHashes()),
			attribute.Bool("pattern.replay_exact", exact),
			attribute.StringSlice("pattern.non_exact_reasons", rec.NonExactReasons),
			attribute.StringSlice("pattern.decisions", rec.DecisionSequence()),
			attribute.String("pattern.flag_digest", rec.FlagDigest),
		))
	}
}

// abortPatternGamesLocked force-ends the in-flight pattern games of the given
// experiments with reason kill_switch (UC-046 A12). Returns the game ids; the
// caller aborts them outside the lock. Assumes r.mu is held.
func (r *Registry) patternGamesLocked(ids []string) []string {
	var games []string
	for _, id := range ids {
		e := r.entries[id]
		if e == nil || !e.patternActive() {
			continue
		}
		for gid := range e.games {
			if !isReservation(gid) {
				games = append(games, gid)
			}
		}
	}
	return games
}

// maxPairReruns bounds how often one pair runs again before its archetype is
// skipped (UC-046 BR-015).
const maxPairReruns = 3

// maxPairPreemptions is the safety valve of BR-015: a pair reruns at most this
// often after a real-game preemption without using up maxPairReruns. A further
// preemption counts like any other release, so an experiment that real games
// preempt again and again still ends (a pair runs at most 1 + 10 + 3 times).
const maxPairPreemptions = 10

// ReleaseReasonPreempted is the ReleaseGame reason of a pattern game a real game
// preempted (coordinator reason preempted_by_real_game, UC-046 A8). Its pair
// reruns without using up maxPairReruns (BR-015).
const ReleaseReasonPreempted = "game ended without usable fitness sample: preempted_by_real_game"

// maxPairStaleReruns is the stale counterpart of maxPairPreemptions (BR-015): a
// pair reruns at most this often after a stale end without using up
// maxPairReruns. A further stale end counts like any other release.
const maxPairStaleReruns = 10

// ReleaseReasonStale is the ReleaseGame reason of a game the agent ended because
// its game_active series went silent (UC-041 A4, #1494). The agent's telemetry
// failed, not the game, so its pair reruns without using up maxPairReruns
// (BR-015), exactly like a preemption, with its own valve maxPairStaleReruns.
const ReleaseReasonStale = "game ended without usable fitness sample: stale"

// Journal notes of BR-015 (KindDecision events without a status change).
const (
	notePairRerun          = "pattern_pair_rerun"
	notePairRerunPreempted = "pattern_pair_rerun_preempted"
	notePairRerunStale     = "pattern_pair_rerun_stale"
	noteArchetypeSkipped   = "pattern_archetype_skipped"
)

// rewindFailedSpawn gives back the arm(s) whose spawn failed: a failed pattern
// spawn uses up no archetype (UC-046 BR-015).
func (r *Registry) rewindFailedSpawn(id string, arms int) {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil || !e.patternActive() || arms <= 0 {
		return
	}
	e.armCursor -= arms
	if e.armCursor < 0 {
		e.armCursor = 0
	}
}

// rerunPairLocked makes a pair whose arm ended without a sample run again, both
// arms, at most maxPairReruns times; then its archetype is skipped (UC-046
// BR-015). A release after a real-game preemption (ReleaseReasonPreempted) reruns
// the pair without using up maxPairReruns, at most maxPairPreemptions times; a
// release after a stale end (ReleaseReasonStale) likewise, at most
// maxPairStaleReruns times. Assumes r.mu is held.
func (r *Registry) rerunPairLocked(e *entry, pairIndex int, reason string) {
	if e == nil || !e.patternActive() || len(e.arms) == 0 || e.pairSkipped[pairIndex] {
		return
	}
	n := len(e.arms)
	pi := pairIndex
	if reason == ReleaseReasonPreempted && e.pairPreemptions[pairIndex] < maxPairPreemptions {
		if e.pairPreemptions == nil {
			e.pairPreemptions = map[int]int{}
		}
		e.pairPreemptions[pairIndex]++
		if e.armCursor > pairIndex*n {
			e.armCursor = pairIndex * n
		}
		r.appendEvent(e.journal, journal.Event{Kind: journal.KindDecision, PairIndex: &pi, Note: notePairRerunPreempted})
		r.log.Info("experiment.pattern_pair_rerun_preempted", "experiment_id", e.id, "pair_index", pairIndex,
			"preemptions", e.pairPreemptions[pairIndex])
		return
	}
	if reason == ReleaseReasonStale && e.pairStaleReruns[pairIndex] < maxPairStaleReruns {
		if e.pairStaleReruns == nil {
			e.pairStaleReruns = map[int]int{}
		}
		e.pairStaleReruns[pairIndex]++
		if e.armCursor > pairIndex*n {
			e.armCursor = pairIndex * n
		}
		r.appendEvent(e.journal, journal.Event{Kind: journal.KindDecision, PairIndex: &pi, Note: notePairRerunStale})
		r.log.Info("experiment.pattern_pair_rerun_stale", "experiment_id", e.id, "pair_index", pairIndex,
			"stale_reruns", e.pairStaleReruns[pairIndex])
		return
	}
	if e.pairReruns == nil {
		e.pairReruns = map[int]int{}
	}
	if e.pairReruns[pairIndex] >= maxPairReruns {
		if e.pairSkipped == nil {
			e.pairSkipped = map[int]bool{}
		}
		e.pairSkipped[pairIndex] = true
		if e.armCursor < (pairIndex+1)*n {
			e.armCursor = (pairIndex + 1) * n
		}
		r.appendEvent(e.journal, journal.Event{Kind: journal.KindDecision, PairIndex: &pi, Note: noteArchetypeSkipped,
			Pattern: &journal.PatternFacts{Archetype: e.pin.Archetypes[pairIndex].Name}})
		r.log.Warn("experiment.pattern_archetype_skipped", "experiment_id", e.id, "pair_index", pairIndex, "reason", reason)
		return
	}
	e.pairReruns[pairIndex]++
	if e.armCursor > pairIndex*n {
		e.armCursor = pairIndex * n
	}
	r.appendEvent(e.journal, journal.Event{Kind: journal.KindDecision, PairIndex: &pi, Note: notePairRerun})
	r.log.Info("experiment.pattern_pair_rerun", "experiment_id", e.id, "pair_index", pairIndex,
		"rerun", e.pairReruns[pairIndex], "reason", reason)
}

// concludeExhaustedPatterns concludes every running pattern experiment that has
// used or skipped all its archetypes and has nothing in flight (UC-046 A6).
func (r *Registry) concludeExhaustedPatterns(ctx context.Context) {
	r.mu.Lock()
	var ids []string
	for id, e := range r.entries {
		if e.status == StatusRunning && e.patternExhaustedLocked() && e.inFlight() == 0 {
			ids = append(ids, id)
		}
	}
	r.mu.Unlock()
	for _, id := range ids {
		r.concludeIfPatternExhausted(ctx, id)
	}
}

// countPatternOutcomeLocked counts a finished pattern game per arm (BR-013).
// Only games that ended with a sample (winner, tie, timeout, agent_ended) enter
// the denominator: an aborted or errored game is released and rerun, so counting
// it would dilute the rates unevenly across arms. Timeouts and agent ends (A14)
// are counted separately.
func (e *entry) countPatternOutcomeLocked(arm, outcome string) {
	switch outcome {
	case patterngame.OutcomeWinner, patterngame.OutcomeTie, patterngame.OutcomeTimeout, patterngame.OutcomeAgentEnded:
	default:
		return
	}
	if e.patternGames == nil {
		e.patternGames = map[string]int{}
		e.patternTimeouts = map[string]int{}
		e.patternAgentEnded = map[string]int{}
	}
	e.patternGames[arm]++
	switch outcome {
	case patterngame.OutcomeTimeout:
		e.patternTimeouts[arm]++
	case patterngame.OutcomeAgentEnded:
		e.patternAgentEnded[arm]++
	}
}

// timeoutSummaryLocked renders the per-arm timeout counts and rates (BR-013), and
// flags an arm that timed out clearly more often: a rate difference of at least
// 0.2 with at least 2 pattern games per arm. "" when no pattern game finished.
func (e *entry) timeoutSummaryLocked() string {
	if len(e.patternGames) == 0 {
		return ""
	}
	arms := append([]string(nil), e.arms...)
	parts := make([]string, 0, len(arms))
	rates := map[string]float64{}
	for _, arm := range arms {
		games, timeouts := e.patternGames[arm], e.patternTimeouts[arm]
		rate := 0.0
		if games > 0 {
			rate = float64(timeouts) / float64(games)
		}
		rates[arm] = rate
		parts = append(parts, fmt.Sprintf("%s %d/%d (%.0f%%)", arm, timeouts, games, rate*100))
	}
	out := "pattern timeouts (right-censored at the cap): " + strings.Join(parts, ", ")
	for _, a := range arms {
		for _, b := range arms {
			if a != b && e.patternGames[a] >= 2 && e.patternGames[b] >= 2 && rates[a]-rates[b] >= 0.2 {
				out += fmt.Sprintf("; the %s arm timed out clearly more often than %s", a, b)
			}
		}
	}
	return out + e.agentEndedSummaryLocked(arms)
}

// agentEndedSummaryLocked renders the per-arm count and rate of games the agent's
// own end_game ended (UC-046 A14, BR-013), reported apart from the timeouts. ""
// when no game ended that way.
func (e *entry) agentEndedSummaryLocked(arms []string) string {
	total := 0
	parts := make([]string, 0, len(arms))
	for _, arm := range arms {
		games, ended := e.patternGames[arm], e.patternAgentEnded[arm]
		total += ended
		rate := 0.0
		if games > 0 {
			rate = float64(ended) / float64(games)
		}
		parts = append(parts, fmt.Sprintf("%s %d/%d (%.0f%%)", arm, ended, games, rate*100))
	}
	if total == 0 {
		return ""
	}
	return "; pattern games ended by the agent's end_game (right-censored): " + strings.Join(parts, ", ")
}

// flagKeyOf returns an experiment's flag key (for teardown outside the lock).
func (r *Registry) flagKeyOf(id string) string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if e := r.entries[id]; e != nil {
		return e.flagKey
	}
	return ""
}

// restorePatternLocked restores a rehydrated experiment's pin and progress from
// its journal (UC-046 BR-015). It returns false when the experiment ran games but
// its pin cannot be restored (the caller discards it). An experiment that never
// pinned (no game assigned yet) is left to pin in its next allocation pass.
// Assumes r.mu is held.
func (r *Registry) restorePatternLocked(e *entry) bool {
	if e.status != StatusRunning {
		return true
	}
	events, err := e.journal.Events()
	if err != nil {
		r.log.Warn("experiment.pattern_restore_failed", "experiment_id", e.id, "error", err)
		return false
	}
	var pinned *journal.PatternFacts
	assigned := false
	for i := range events {
		ev := events[i]
		switch {
		case ev.Kind == journal.KindPatternPinned && ev.Pattern != nil:
			pinned = ev.Pattern
		case ev.Kind == journal.KindGameAssigned:
			assigned = true
		}
	}
	if pinned == nil {
		return !assigned
	}
	pin, err := patterngame.RestorePin(pinned.Selection, pinned.Archetypes, pinned.ParamsJSON, pinned.ParamsDigests, pinned.MaxGameSeconds)
	if err != nil {
		r.log.Warn("experiment.pattern_pin_lost", "experiment_id", e.id, "error", err)
		return false
	}
	e.pin = &pin
	// Progress: a pair is done when each arm concluded since its last rerun, or skipped.
	n := len(e.arms)
	done := map[int]map[string]bool{}
	for _, ev := range events {
		if ev.PairIndex == nil {
			continue
		}
		p := *ev.PairIndex
		switch {
		case ev.Kind == journal.KindDecision && ev.Note == notePairRerun:
			if e.pairReruns == nil {
				e.pairReruns = map[int]int{}
			}
			e.pairReruns[p]++
			delete(done, p)
		case ev.Kind == journal.KindDecision && ev.Note == notePairRerunPreempted:
			if e.pairPreemptions == nil {
				e.pairPreemptions = map[int]int{}
			}
			e.pairPreemptions[p]++
			delete(done, p)
		case ev.Kind == journal.KindDecision && ev.Note == notePairRerunStale:
			if e.pairStaleReruns == nil {
				e.pairStaleReruns = map[int]int{}
			}
			e.pairStaleReruns[p]++
			delete(done, p)
		case ev.Kind == journal.KindDecision && ev.Note == noteArchetypeSkipped:
			if e.pairSkipped == nil {
				e.pairSkipped = map[int]bool{}
			}
			e.pairSkipped[p] = true
		case ev.Kind == journal.KindGameConcluded:
			if done[p] == nil {
				done[p] = map[string]bool{}
			}
			done[p][ev.Arm] = true
		case ev.Kind == journal.KindPatternGameEnded && ev.Pattern != nil:
			e.countPatternOutcomeLocked(ev.Arm, ev.Pattern.Outcome)
		}
	}
	p := 0
	for p < len(pin.Archetypes) && (e.pairSkipped[p] || len(done[p]) >= n) {
		p++
	}
	e.armCursor = p * n
	r.log.Info("experiment.pattern_pin_restored", "experiment_id", e.id,
		"archetypes", pin.Names(), "resume_pair", p)
	return true
}
