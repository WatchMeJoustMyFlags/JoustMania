package promote

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
)

// deferred.go: the deferred real writes of hold keys (UC-043 A17 step 3, FR-162,
// #2096). The maintainer rule: the agent changes a shield.* setting only at the end
// of a game, never during one; the kill switch reverts at any time.
//
//   - An autonomous promotion of a hold key while a real game is live (the probe, a
//     telemetry prefilter, or the flag service's authoritative 409 real_game_live)
//     returns OutcomeDeferred and waits here, one entry per flag. A newer promotion
//     of the flag supersedes it (journaled discarded: superseded).
//   - The experiment loop calls DrainDeferred every DeferredDrainInterval. With no
//     real game live it writes each waiting entry with the promotion's original
//     kill-switch epoch, watches it (applyAndWatch) and journals the outcome. An
//     entry older than DeferredTTL, a mode other than autonomous, a post-revert
//     cooldown and a failed write drop it, journaled discarded.
//   - A watch or re-watch revert of a hold key while a real game is live waits the
//     same way (revertReal); DrainDeferred runs it once no real game is live.
//   - The kill switch drops every waiting write (returned for its journal, outside
//     killMu) and every waiting revert (it reverts the overlay itself);
//     DropDeferred journals what a graceful shutdown drops. A crash loses the
//     queue: the journal ends at deferred and nothing was written (fail safe).
//
// The queue is guarded by mu. Enqueueing a write takes killMu (epoch check, as
// writeTracked), so a write never enters the queue after a kill-switch tick that
// its config predates. Nothing journals under killMu.

// RealGameProbe reports whether a real game is live. An error counts as live.
type RealGameProbe func(ctx context.Context) (live bool, err error)

const (
	// DeferredTTL bounds how long a promotion write waits: its FitnessBefore
	// baseline goes stale.
	DeferredTTL = 24 * time.Hour
	// DeferredDrainInterval is how often the experiment loop drains the queue.
	DeferredDrainInterval = 2 * time.Second

	// DeferredReason is the Reason of an OutcomeDeferred promotion.
	DeferredReason = "real game running; real write waits for game end (UC-043 A17)"
	// DeferredRevertReason is the Reason a watch revert that waits leaves on its result.
	DeferredRevertReason = "revert deferred until no real game runs (UC-043 A17)"
	// DeferredAppliedReason is the Reason journaled when a waiting write lands.
	DeferredAppliedReason = "deferred real write applied after the real game ended (UC-043 A17)"

	// Reasons a waiting write is dropped (journaled discarded).
	ReasonSuperseded      = "superseded"
	ReasonDeferredExpired = "deferred_expired"
	ReasonModeChanged     = "mode_changed"
	ReasonShutdown        = "shutdown"
)

// deferredWrite is one promotion write waiting until no real game runs.
type deferredWrite struct {
	proposal  experiment.Proposal
	evidence  Evidence
	killEpoch uint64
	queuedAt  time.Time
}

// deferredRevert is one watch or re-watch revert waiting until no real game runs.
type deferredRevert struct {
	proposal experiment.Proposal
	entry    *appliedPromotion
	watchErr error
	rewatch  bool
	queuedAt time.Time
}

// deferUntilIdle reports whether flagKey's real value may change only while no
// real game runs (the hold keys, UC-043 A17).
func deferUntilIdle(flagKey string) bool { return experiment.IsHoldKey(flagKey) }

// SetRealGameProbe wires the real-game probe. Without one every hold-key write and
// watch revert waits (fail closed).
func (pr *Promoter) SetRealGameProbe(p RealGameProbe) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	pr.probe = p
}

// HasRealGameProbe reports whether a real-game probe is wired.
func (pr *Promoter) HasRealGameProbe() bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.probe != nil
}

// realGameLive asks the probe; no probe or an error counts as live (fail closed).
func (pr *Promoter) realGameLive(ctx context.Context) bool {
	pr.mu.Lock()
	probe := pr.probe
	pr.mu.Unlock()
	if probe == nil {
		return true
	}
	live, err := probe(ctx)
	if err != nil {
		pr.log.Debug("real-game probe failed; treating a real game as live (UC-043 A17)", "error", err)
		return true
	}
	return live
}

// enqueueDeferred queues a promotion write. It refuses one whose config predates a
// kill-switch tick (as writeTracked). A waiting write of the same flag is replaced
// and returned, for the caller to journal outside killMu.
func (pr *Promoter) enqueueDeferred(p experiment.Proposal, ev Evidence, cfg Config) (*deferredWrite, error) {
	pr.killMu.Lock()
	defer pr.killMu.Unlock()
	if pr.KillSwitchEpoch() != cfg.killEpoch {
		return nil, errKillSwitched
	}
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.pending == nil {
		pr.pending = map[string]*deferredWrite{}
	}
	old := pr.pending[p.FlagKey]
	pr.pending[p.FlagKey] = &deferredWrite{proposal: p, evidence: ev, killEpoch: cfg.killEpoch, queuedAt: pr.clock()}
	return old, nil
}

// deferPromotion queues p's write and sets res to deferred (or discarded when the
// kill switch ran since cfg). A superseded write is journaled.
func (pr *Promoter) deferPromotion(p experiment.Proposal, ev Evidence, cfg Config, res *Result) {
	old, err := pr.enqueueDeferred(p, ev, cfg)
	if err != nil {
		res.Outcome = OutcomeDiscarded
		res.Reason = err.Error()
		return
	}
	res.Outcome = OutcomeDeferred
	res.Reason = DeferredReason
	pr.log.Warn("code_improvement.deferred: real write waits until no real game runs (UC-043 A17)",
		"flag", p.FlagKey, "experiment_id", p.ExperimentID)
	if old != nil {
		pr.journalDeferred(old.proposal, OutcomeDiscarded, ReasonSuperseded)
	}
}

// queueRevertLocked queues a watch or re-watch revert. Assumes killMu is held.
func (pr *Promoter) queueRevertLocked(p experiment.Proposal, entry *appliedPromotion, watchErr error, rewatch bool) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	if pr.pendingReverts == nil {
		pr.pendingReverts = map[string]*deferredRevert{}
	}
	pr.pendingReverts[p.FlagKey] = &deferredRevert{proposal: p, entry: entry, watchErr: watchErr, rewatch: rewatch, queuedAt: pr.clock()}
}

// PendingFlags lists the flags with a waiting promotion write. The registry counts
// them as under test, so no experiment is declared on them meanwhile (UC-043 A17).
func (pr *Promoter) PendingFlags() []string {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	out := make([]string, 0, len(pr.pending))
	for flag := range pr.pending {
		out = append(out, flag)
	}
	sort.Strings(out)
	return out
}

// DeferredStats returns the number of waiting writes and reverts and the age of
// the oldest one (0 when none), for the gauges.
func (pr *Promoter) DeferredStats() (pending int, oldest time.Duration) {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	now := pr.clock()
	note := func(at time.Time) {
		pending++
		if age := now.Sub(at); age > oldest {
			oldest = age
		}
	}
	for _, w := range pr.pending {
		note(w.queuedAt)
	}
	for _, r := range pr.pendingReverts {
		note(r.queuedAt)
	}
	return pending, oldest
}

// DrainDeferred runs one pass over the queue (the experiment loop calls it every
// DeferredDrainInterval with the live promotion config). It drops expired writes
// whatever the probe says, drops every write when the mode is not autonomous or the
// agent is disabled, and otherwise, with no real game live, runs the waiting
// reverts and then the waiting writes. It journals each outcome through the
// promotion journal; it must not be called under killMu.
func (pr *Promoter) DrainDeferred(ctx context.Context, cfg Config) {
	now := pr.clock()
	switch {
	case !cfg.Enabled:
		pr.dropWrites(KillSwitchReason, nil)
	case normalizeMode(cfg.Mode) != ModeAutonomous:
		pr.dropWrites(ReasonModeChanged, nil)
	default:
		pr.dropWrites(ReasonDeferredExpired, func(w *deferredWrite) bool { return now.Sub(w.queuedAt) >= DeferredTTL })
	}
	if !pr.hasDeferred() || pr.realGameLive(ctx) {
		return
	}
	pr.drainReverts(ctx)
	pr.drainWrites(ctx)
}

// DropDeferred drops every waiting write (journaled discarded with reason) and
// every waiting revert (journaled applied: the real default stays until the kill
// switch or an operator removes it). A graceful shutdown calls it before the
// journal closes.
func (pr *Promoter) DropDeferred(reason string) {
	pr.dropWrites(reason, nil)
	pr.mu.Lock()
	reverts := pr.pendingReverts
	pr.pendingReverts = nil
	pr.mu.Unlock()
	for _, flag := range sortedKeys(reverts) {
		r := reverts[flag]
		pr.journalDeferred(r.proposal, OutcomeApplied,
			reason+": deferred revert not run; the real default stays until the kill switch removes it")
	}
}

// dropWrites removes the waiting writes that match (all when match is nil) and
// journals each discarded with reason.
func (pr *Promoter) dropWrites(reason string, match func(*deferredWrite) bool) {
	pr.mu.Lock()
	var dropped []*deferredWrite
	for _, flag := range sortedKeys(pr.pending) {
		if w := pr.pending[flag]; match == nil || match(w) {
			dropped = append(dropped, w)
			delete(pr.pending, flag)
		}
	}
	pr.mu.Unlock()
	for _, w := range dropped {
		pr.journalDeferred(w.proposal, OutcomeDiscarded, reason)
	}
}

// takeKillSwitchedLocked empties the queue for the kill switch: the waiting writes
// are returned as discarded items for its journal, the waiting reverts are dropped
// (the kill switch reverts every promotion overlay itself). Assumes killMu is held.
func (pr *Promoter) takeKillSwitchedLocked() []KillSwitchRevert {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	out := make([]KillSwitchRevert, 0, len(pr.pending))
	for _, flag := range sortedKeys(pr.pending) {
		w := pr.pending[flag]
		out = append(out, KillSwitchRevert{ExperimentID: w.proposal.ExperimentID, FlagKey: flag,
			Outcome: OutcomeDiscarded, Reason: KillSwitchReason})
	}
	pr.pending = nil
	pr.pendingReverts = nil
	return out
}

func (pr *Promoter) hasDeferred() bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return len(pr.pending) > 0 || len(pr.pendingReverts) > 0
}

// drainReverts runs the waiting reverts. revertReal checks the probe again and
// queues a revert back when a real game started meanwhile; a revert the kill
// switch or a newer promotion took over is dropped silently (they journal it).
func (pr *Promoter) drainReverts(ctx context.Context) {
	pr.mu.Lock()
	reverts := pr.pendingReverts
	pr.pendingReverts = nil
	pr.mu.Unlock()
	for _, flag := range sortedKeys(reverts) {
		r := reverts[flag]
		res := Result{Outcome: OutcomeApplied}
		if pr.revertReal(ctx, r.proposal, r.entry, &res, r.watchErr, r.rewatch) != claimOwned {
			continue
		}
		reason := res.Reason + " (deferred until no real game ran)"
		if r.rewatch {
			reason = RewatchReasonPrefix + reason
		}
		pr.journalDeferred(r.proposal, res.Outcome, reason)
	}
}

// drainWrites writes the waiting promotions, oldest flag order first by key. A
// 409 real_game_live (a real game registered after the probe) puts the entry back
// and ends the pass.
func (pr *Promoter) drainWrites(ctx context.Context) {
	pr.mu.Lock()
	flags := sortedKeys(pr.pending)
	pr.mu.Unlock()
	for _, flag := range flags {
		pr.mu.Lock()
		w := pr.pending[flag]
		delete(pr.pending, flag)
		pr.mu.Unlock()
		if w == nil {
			continue
		}
		if remaining, cooling := pr.cooldownRemaining(flag); cooling {
			pr.journalDeferred(w.proposal, OutcomeDiscarded, fmt.Sprintf(
				"deferred re-promotion of %q suppressed: %s remaining in post-revert cooldown (#1065)", flag, remaining.Round(time.Second)))
			continue
		}
		cfg := Config{Mode: ModeAutonomous, Enabled: true, killEpoch: w.killEpoch, epochKnown: true}
		entry, err := pr.writeTracked(ctx, w.proposal, w.evidence.RealDefaultValue, cfg)
		switch {
		case flagservice.IsReason(err, flagservice.ReasonRealGameLive):
			pr.requeue(w)
			return
		case errors.Is(err, errKillSwitched):
			pr.journalDeferred(w.proposal, OutcomeDiscarded, KillSwitchReason)
			continue
		case err != nil:
			pr.journalDeferred(w.proposal, OutcomeDiscarded, err.Error())
			continue
		}
		res := Result{Mode: ModeAutonomous, Outcome: OutcomeApplied, Reason: DeferredAppliedReason}
		pr.applyAndWatch(ctx, w.proposal, w.evidence, entry, &res)
		pr.log.Warn("code_improvement.deferred_applied (UC-043 A17)", "flag", flag,
			"experiment_id", w.proposal.ExperimentID, "outcome", string(res.Outcome), "reason", res.Reason)
		pr.journalDeferred(w.proposal, res.Outcome, res.Reason)
	}
}

// requeue puts back a write the flag service refused, unless a newer promotion of
// the flag took its place (then it is superseded).
func (pr *Promoter) requeue(w *deferredWrite) {
	pr.mu.Lock()
	if pr.pending == nil {
		pr.pending = map[string]*deferredWrite{}
	}
	_, newer := pr.pending[w.proposal.FlagKey]
	if !newer {
		pr.pending[w.proposal.FlagKey] = w
	}
	pr.mu.Unlock()
	if newer {
		pr.journalDeferred(w.proposal, OutcomeDiscarded, ReasonSuperseded)
	}
}

// journalDeferred logs a later outcome of a deferred promotion and journals it on
// its experiment through the promotion journal (Registry.RecordPromotion).
func (pr *Promoter) journalDeferred(p experiment.Proposal, outcome Outcome, reason string) {
	pr.log.Warn("code_improvement.deferred_outcome (UC-043 A17)", "flag", p.FlagKey,
		"experiment_id", p.ExperimentID, "outcome", string(outcome), "reason", reason)
	pr.mu.Lock()
	sink := pr.rewatchJournal
	pr.mu.Unlock()
	if sink == nil || p.ExperimentID == "" {
		return
	}
	sink(p.ExperimentID, experiment.PromotionResult{Outcome: string(outcome), Reason: reason})
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
