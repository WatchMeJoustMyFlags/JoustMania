package promote

import (
	"context"
	"encoding/json"

	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"

	"errors"
	"fmt"
	"reflect"
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
	// restored: re-queued from the journal after a restart (no entry); it runs only
	// while the promotion overlay still holds proposal.ExperimentalValue.
	restored bool
}

// PromotionOverlayReader reads the value of a flag's promotion-written real
// overlay (ok false: none). The production reverter implements it.
type PromotionOverlayReader interface {
	PromotionOverlay(ctx context.Context, flagKey string) (value json.RawMessage, ok bool, err error)
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
		pr.noteProbeError(ctx, err)
		return true
	}
	return live
}

// ProbeWarnEvery rate-limits the WARN of a failing real-game probe.
const ProbeWarnEvery = time.Minute

// noteProbeError counts a failed probe read and logs a WARN at most once per
// ProbeWarnEvery: a probe that keeps failing makes every hold-key write and revert
// wait (fail closed), which must be visible (UC-043 A17).
func (pr *Promoter) noteProbeError(ctx context.Context, err error) {
	pr.noteReadError(ctx, ProbeErrorSourceProbe, err)
}

// Sources of agent_promotion_probe_errors_total (attribute `source`).
const (
	ProbeErrorSourceProbe   = "probe"
	ProbeErrorSourceOverlay = "overlay_read"
)

// noteReadError counts a failed read that makes a deferred entry wait (the
// real-game probe, or the overlay read of a restored revert) and logs a WARN at
// most once per ProbeWarnEvery and source.
func (pr *Promoter) noteReadError(ctx context.Context, source string, err error) {
	pr.mu.Lock()
	counter := pr.probeErrors
	now := pr.clock()
	if pr.lastProbeWarn == nil {
		pr.lastProbeWarn = map[string]time.Time{}
	}
	last, seen := pr.lastProbeWarn[source]
	warn := !seen || now.Sub(last) >= ProbeWarnEvery
	if warn {
		pr.lastProbeWarn[source] = now
	}
	pr.mu.Unlock()
	if counter != nil {
		counter.Add(ctx, 1, otelmetric.WithAttributes(attribute.String("source", source)))
	}
	if warn {
		pr.log.Warn("deferred promotion read failed; hold-key writes and reverts wait (UC-043 A17, fail closed)",
			"source", source, "error", err)
	}
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
	if pr.closed {
		return // shutting down: the write-ahead journal event re-queues it next run
	}
	if entry == nil {
		// A restored revert (RestoreDeferredReverts): kept beside any other restored
		// revert of the flag; the drain's value check decides which one runs.
		pr.restoredReverts = append(pr.restoredReverts, &deferredRevert{
			proposal: p, rewatch: rewatch, queuedAt: pr.clock(), restored: true,
		})
		return
	}
	if pr.pendingReverts == nil {
		pr.pendingReverts = map[string]*deferredRevert{}
	}
	if old := pr.pendingReverts[p.FlagKey]; old != nil && old.proposal.ExperimentID != p.ExperimentID {
		// Another experiment's waiting revert of the flag is replaced: its pending
		// state is cleared in the journal outside killMu (flushReplacedReverts).
		pr.replacedReverts = append(pr.replacedReverts, old.proposal)
	}
	pr.pendingReverts[p.FlagKey] = &deferredRevert{proposal: p, entry: entry, watchErr: watchErr, rewatch: rewatch, queuedAt: pr.clock()}
}

// ReasonRevertSuperseded clears the pending state of a revert a newer promotion
// (or another experiment's revert) of the flag took over.
const ReasonRevertSuperseded = "superseded by a newer promotion of the flag; not reverted"

// flushReplacedReverts journals a clearing event (no RevertPending) for each
// waiting revert another one replaced, so a restart never re-queues it. It must
// not be called under killMu.
func (pr *Promoter) flushReplacedReverts() {
	pr.mu.Lock()
	replaced := pr.replacedReverts
	pr.replacedReverts = nil
	pr.mu.Unlock()
	for _, p := range replaced {
		pr.journalDeferred(p, OutcomeApplied, ReasonRevertSuperseded)
	}
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
	for _, r := range pr.restoredReverts {
		note(r.queuedAt)
	}
	return pending, oldest
}

// DrainDeferred runs one pass over the queue (the experiment loop calls it every
// DeferredDrainInterval). resolve reads the live promotion config. Expired writes
// drop whatever the probe says. A mode other than autonomous (or the agent
// disabled) on two consecutive passes drops every waiting write; a single such pass
// only skips the writes, so a flagd sync flap (which reads the fail-closed `issue`)
// does not drop them. With no real game live it runs the waiting reverts (whatever
// the mode: a revert only restores) and then the waiting writes, re-reading the
// mode before each write. It journals each outcome through the promotion journal;
// it must not be called under killMu.
func (pr *Promoter) DrainDeferred(ctx context.Context, resolve ConfigResolver) {
	now := pr.clock()
	cfg := resolve(ctx)
	off := !promotionOn(cfg)
	pr.mu.Lock()
	if off {
		pr.offPasses++
	} else {
		pr.offPasses = 0
	}
	dropAll := off && pr.offPasses >= 2
	pr.mu.Unlock()
	switch {
	case dropAll && !cfg.Enabled:
		pr.dropWrites(KillSwitchReason, nil)
	case dropAll:
		pr.dropWrites(ReasonModeChanged, nil)
	default:
		pr.dropWrites(ReasonDeferredExpired, func(w *deferredWrite) bool { return now.Sub(w.queuedAt) >= DeferredTTL })
	}
	if !pr.hasDeferred() || pr.realGameLive(ctx) {
		return
	}
	pr.drainReverts(ctx)
	if !off {
		pr.drainWrites(ctx, resolve)
	}
}

// promotionOn reports whether cfg allows an autonomous real write.
func promotionOn(cfg Config) bool {
	return cfg.Enabled && normalizeMode(cfg.Mode) == ModeAutonomous
}

// DropDeferred is the graceful shutdown of the queue (UC-043 A17 step 3). Every
// waiting write is dropped, journaled discarded with reason. A waiting revert runs
// now when no real game is live (allowed between games); otherwise it stays
// unrun, and its write-ahead journal event (RevertPending) lets the next agent run
// re-queue it. Reverts queued after this call are not kept (closed).
func (pr *Promoter) DropDeferred(ctx context.Context, reason string) {
	pr.mu.Lock()
	pr.closed = true
	pr.mu.Unlock()
	pr.dropWrites(reason, nil)
	if !pr.hasDeferred() {
		return
	}
	if !pr.realGameLive(ctx) {
		pr.drainReverts(ctx)
		return
	}
	pr.mu.Lock()
	left := sortedKeys(pr.pendingReverts)
	for _, r := range pr.restoredReverts {
		left = append(left, r.proposal.FlagKey)
	}
	pr.pendingReverts = nil
	pr.restoredReverts = nil
	pr.mu.Unlock()
	pr.log.Warn("code_improvement: deferred reverts left for the next run (a real game is live at shutdown, UC-043 A17)", "flags", left)
}

// RestoreDeferredReverts re-queues the waiting reverts a previous run journaled
// (experiment.Registry.TakeRestoredReverts, UC-043 A17 step 3). The drain runs one
// at the first check with no real game live, and only while the flag's promotion
// overlay still holds the experiment's promoted value (restoredRevertStale); an
// unreadable overlay keeps it waiting.
func (pr *Promoter) RestoreDeferredReverts(ctx context.Context, restored []experiment.RestoredRevert) {
	if len(restored) == 0 {
		return
	}
	pr.killMu.Lock()
	defer pr.killMu.Unlock()
	for _, r := range restored {
		p := experiment.Proposal{FlagKey: r.FlagKey, ExperimentID: r.ExperimentID, ExperimentalValue: r.Value}
		pr.queueRevertLocked(p, nil, nil, r.Rewatch)
	}
}

// restoredRevertStale reports why a restored revert must not run (the overlay is
// gone or holds another value), "" when it may; keep is true when the overlay
// could not be read (the revert waits for the next pass).
func (pr *Promoter) restoredRevertStale(ctx context.Context, r *deferredRevert) (why string, keep bool) {
	reader, ok := pr.reverter.(PromotionOverlayReader)
	if !ok {
		return "", false
	}
	raw, present, err := reader.PromotionOverlay(ctx, r.proposal.FlagKey)
	switch {
	case err != nil:
		pr.noteReadError(ctx, ProbeErrorSourceOverlay, err)
		return "", true
	case !present:
		return "revert not run: no promotion overlay left (UC-043 A17)", false
	case !sameJSONValue(raw, r.proposal.ExperimentalValue):
		return "revert not run: the overlay holds a newer promotion's value (UC-043 A17)", false
	}
	return "", false
}

// sameJSONValue compares a raw JSON value with a Go value by their JSON meaning.
func sameJSONValue(raw json.RawMessage, v any) bool {
	want, err := json.Marshal(v)
	if err != nil {
		return false
	}
	var a, b any
	if json.Unmarshal(raw, &a) != nil || json.Unmarshal(want, &b) != nil {
		return false
	}
	return reflect.DeepEqual(a, b)
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
	pr.restoredReverts = nil
	return out
}

func (pr *Promoter) hasDeferred() bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return len(pr.pending) > 0 || len(pr.pendingReverts) > 0 || len(pr.restoredReverts) > 0
}

// drainReverts runs the waiting reverts. revertReal checks the probe again and
// queues a revert back when a real game started meanwhile; a revert the kill
// switch or a newer promotion took over is dropped silently (they journal it).
func (pr *Promoter) drainReverts(ctx context.Context) {
	pr.mu.Lock()
	keyed := pr.pendingReverts
	pr.pendingReverts = nil
	reverts := make([]*deferredRevert, 0, len(keyed)+len(pr.restoredReverts))
	for _, flag := range sortedKeys(keyed) {
		reverts = append(reverts, keyed[flag])
	}
	reverts = append(reverts, pr.restoredReverts...)
	pr.restoredReverts = nil
	pr.mu.Unlock()
	for _, r := range reverts {
		flag := r.proposal.FlagKey
		res := Result{Outcome: OutcomeApplied}
		if r.entry == nil && pr.trackedPromotion(flag) {
			// A restored revert (no entry): a newer promotion of this run owns the
			// overlay. Journaled without RevertPending, which clears the pending state.
			pr.journalDeferred(r.proposal, OutcomeApplied, ReasonRevertSuperseded)
			continue
		}
		if r.restored {
			why, keep := pr.restoredRevertStale(ctx, r)
			if keep {
				pr.mu.Lock()
				pr.restoredReverts = append(pr.restoredReverts, r)
				pr.mu.Unlock()
				continue
			}
			if why != "" {
				pr.journalDeferred(r.proposal, OutcomeApplied, why)
				continue
			}
		}
		switch pr.revertReal(ctx, r.proposal, r.entry, &res, r.watchErr, r.rewatch) {
		case claimOwned:
		case claimSuperseded:
			// A newer promotion of the flag replaced the one this revert was for:
			// clear the pending state so a restart does not re-queue it.
			pr.journalDeferred(r.proposal, OutcomeApplied, ReasonRevertSuperseded)
			continue
		default: // killed: the kill switch journals it; deferred: still waiting
			continue
		}
		reason := res.Reason + " (deferred until no real game ran)"
		if r.rewatch {
			reason = RewatchReasonPrefix + reason
		}
		pr.journalDeferred(r.proposal, res.Outcome, reason)
	}
}

// trackedPromotion reports whether this run tracks an applied promotion of flag.
func (pr *Promoter) trackedPromotion(flag string) bool {
	pr.mu.Lock()
	defer pr.mu.Unlock()
	return pr.applied[flag] != nil
}

// drainWrites writes the waiting promotions in flag order. The mode is re-read
// before each write; a mode other than autonomous puts the entry back and ends the
// pass (the remaining gap is a flip and back between that read and the write). A
// 409 real_game_live (a real game registered after the probe) puts it back too.
//
// The write uses the promotion's original kill-switch epoch, so a kill-switch tick
// since the promotion refuses it. The kill switch also empties the queue, and in
// production it runs on the same experiment-loop goroutine as this drain, so the
// two checks agree.
func (pr *Promoter) drainWrites(ctx context.Context, resolve ConfigResolver) {
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
		if !promotionOn(resolve(ctx)) {
			pr.requeue(w)
			return
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
		// A degradation verdict during a real game leaves its revert waiting:
		// journal that write-ahead (RevertPending), so a restart re-queues it.
		pr.journalDeferredResult(w.proposal, experiment.PromotionResult{
			Outcome: string(res.Outcome), Reason: res.Reason, RevertPending: res.RevertPending,
		})
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
	pr.journalDeferredResult(p, experiment.PromotionResult{Outcome: string(outcome), Reason: reason})
}

// journalDeferredResult is journalDeferred with the full promotion result.
func (pr *Promoter) journalDeferredResult(p experiment.Proposal, res experiment.PromotionResult) {
	outcome, reason := res.Outcome, res.Reason
	pr.log.Warn("code_improvement.deferred_outcome (UC-043 A17)", "flag", p.FlagKey,
		"experiment_id", p.ExperimentID, "outcome", outcome, "reason", reason)
	pr.mu.Lock()
	sink := pr.rewatchJournal
	pr.mu.Unlock()
	if sink == nil || p.ExperimentID == "" {
		return
	}
	sink(p.ExperimentID, res)
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
