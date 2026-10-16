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

// PatternPinner resolves the experiment.mock_archetypes selection at an
// experiment's first spawn (UC-046 step 2). Production reads the live agent flags;
// nil means no pattern games ever (today's noise).
type PatternPinner interface {
	Pin(ctx context.Context) patterngame.Pin
}

// PatternSpawn is what a pattern arm's Spawn needs; it rides on the spawn ctx so
// the ShadowSpawner seam keeps its signature.
type PatternSpawn struct {
	Archetype      patterngame.Archetype
	MaxGameSeconds int
	PairIndex      int
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
	for id, e := range r.entries {
		if e.status == StatusRunning && e.pin == nil {
			pending = append(pending, id)
		}
	}
	r.mu.Unlock()

	for _, id := range pending {
		pin := r.patterns.Pin(ctx)
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
				Pattern: &journal.PatternFacts{
					Selection:      pin.Selection,
					Archetypes:     pin.Names(),
					ParamsDigests:  pin.Digests(),
					MaxGameSeconds: pin.MaxGameSeconds,
				},
			})
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
func (e *entry) patternFactsLocked(pairIndex int) *journal.PatternFacts {
	if !e.patternActive() || pairIndex < 0 || pairIndex >= len(e.pin.Archetypes) {
		return nil
	}
	a := e.pin.Archetypes[pairIndex]
	return &journal.PatternFacts{
		Archetype:    a.Name,
		ParamsDigest: a.Digest,
		RngSeed:      pairSeed(e.id, pairIndex),
	}
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
	facts := &journal.PatternFacts{
		Archetype:        rec.Archetype.Name,
		ParamsDigest:     rec.Archetype.Digest,
		RngSeed:          rec.Seed,
		FlagDigest:       rec.FlagDigest,
		SlotSerials:      rec.Serials,
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
