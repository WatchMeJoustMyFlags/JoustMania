package decision

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/infracontext"
)

// InfraEvaluator is the seam the infrastructure observe path triggers when the
// Bluetooth-health context updates. InfraLoop is the real fitness + rollout
// expansion implementation behind it (#734).
//
// OnInfraEvaluate receives the inbound gRPC context (for trace propagation) and
// an isolated InfraContext snapshot.
type InfraEvaluator interface {
	OnInfraEvaluate(ctx context.Context, snap infracontext.InfraContext)
}

// RolloutActuator is the write seam the InfraLoop uses to advance the rollout
// stage. It is satisfied by actions.RolloutWriter (which flips the
// current_controller_count defaultVariant in services/flagd/rollout.json) and by
// a fake in tests. The actions package imports decision, so the dependency runs
// loop → interface ← actions; the loop never imports actions.
//
// The ladder helpers live here too (NextStage / StageVariantForValue): the loop
// reasons over controller-count VALUES but flagd flips by VARIANT NAME, so the
// actuator owns the value↔variant mapping that bridges the two.
type RolloutActuator interface {
	// SetControllerCount flips current_controller_count's defaultVariant to the
	// named variant; an unknown variant is an error and nothing is written.
	SetControllerCount(variant string) error
	// NextStage returns the next stage up the ladder from a controller-count
	// value: the variant name, its value, and ok (false at/above the terminal
	// stage or for an unknown current value).
	NextStage(current int) (variant string, value int, ok bool)
	// StageVariantForValue maps a controller-count value to its ladder variant
	// name; ok is false for any value not on the ladder.
	StageVariantForValue(value int) (string, bool)
	// DryRun reports whether SetControllerCount is a no-op (the rehearsal
	// actuator) rather than an applied write. The loop lifts it onto every
	// decision span as rollout.dry_run so a Jaeger consumer can tell a rehearsed
	// "expand"/"rollback" apart from one that actually flipped current_controller_count.
	// RolloutWriter → false (real); DryRunRolloutWriter → true.
	DryRun() bool
}

// RolloutApplier is optionally implemented by an actuator whose dry-run state is
// read LIVE (actions.GatedRolloutActuator reads rollout_enabled per call). A
// separate DryRun() then SetControllerCount() reads the gate twice, so a flag
// flip in between could make the loop commit state for a write that never
// happened (or skip it for one that did). ApplyControllerCount does the gate read
// and the write together and reports which one happened: applied=true means the
// flip reached the flag service, false means it was a dry run.
type RolloutApplier interface {
	ApplyControllerCount(variant string) (applied bool, err error)
}

// ServedRolloutStage is what the flag service serves for current_controller_count
// right now, as RolloutStageReader reports it.
type ServedRolloutStage struct {
	// Value is the served controller count; meaningful only when OnLadder.
	Value int
	// OnLadder is false when the served variant is not a ladder stage.
	OnLadder bool
	// ByAgent is true when the served value is the agent's own stack overlay. A
	// value set by an operator or the stage scripts, or the baseline after the
	// overlay was cleared, is false.
	ByAgent bool
	// Epoch is the flag service's boot epoch ("" when it could not be read). A
	// different epoch than the one seen earlier means the service restarted and
	// dropped every overlay, the agent's last write included.
	Epoch string
}

// RolloutStageReader is optionally implemented by an actuator that can read back
// the stage the flag service serves (actions.RolloutWriter and
// actions.GatedRolloutActuator). The loop uses it to notice that its in-memory
// rollout state no longer matches the flag service: an operator reset the stage,
// or the flag service restarted (#1384). An actuator without it keeps the old
// behaviour (the state is cleared only when the rollout becomes inactive).
type RolloutStageReader interface {
	ServedStage() (ServedRolloutStage, error)
}

// RemediationSource resolves the `remediation_allowed` gate that decides
// whether the loop may auto-roll-back (write the rollout file) or may only
// RECOMMEND a rollback (span only). It is the infra-domain parallel to the
// agent-domain permission gate, but `remediation_allowed` lives in the rollout
// flagd domain (rollout.json), not the agent domain — so it is sourced
// separately from the per-cycle agent Snapshot.
//
// It is satisfied by actions.RemediationReader (which resolves the flag's
// defaultVariant from rollout.json each call) in production, and by a static
// fake in tests. RemediationAllowed MUST be fail-closed: any error returns
// false (recommend-only), so a missing/garbled control plane never auto-writes.
type RemediationSource interface {
	RemediationAllowed() bool
}

// staticRemediation is a constant RemediationSource (test/default helper).
type staticRemediation bool

func (s staticRemediation) RemediationAllowed() bool { return bool(s) }

// StaticRemediation returns a RemediationSource that always reports allowed. It
// is exported for tests and for any caller that wants a constant gate.
func StaticRemediation(allowed bool) RemediationSource { return staticRemediation(allowed) }

// rolloutGate decides whether the infrastructure observe path should evaluate at
// all this cycle (≥1 controller reporting fresh health). It is the infra-domain
// parallel to gate.ShouldEvaluate, injected so the loop stays unit-testable
// without importing the gate package (which would be a layering inversion).
type rolloutGate func(snap infracontext.InfraContext, now time.Time) bool

// DefaultRolloutDwell is the minimum time the rollout must dwell at a stage with
// passing fitness before the loop expands to the next stage. Each new stage adds
// controllers to the target backend; fitness needs a few health windows to
// observe whether the larger set is still healthy before we expand again. Tunable
// via AGENT_ROLLOUT_DWELL_SECONDS (read at construction in main.go).
const DefaultRolloutDwell = 15 * time.Second

// Remediation action values recorded on remediation.action of the rollout
// decision span. The set is CLOSED — exactly these four values — because PR G's
// narrative test discriminates cycles by this attribute:
//
//   - none: no remediation taken this cycle. Either fitness passes and the loop
//     is observing/holding/terminal (PR E), or fitness fails but there is
//     nothing to roll back (stage already none), or a rollback is already in
//     flight (the settling window — the write happened, the count has not yet
//     returned to 0).
//   - expand: the loop advanced the rollout one stage (PR E).
//   - rollback: fitness failed at an active stage (>0), rollback was ALLOWED,
//     and the loop reset current_controller_count toward "none" (PR F). In
//     dry-run this is "decided but not applied".
//   - recommended_only: fitness failed at an active stage (>0) but rollback was
//     NOT allowed (remediation_allowed=false); the loop records the recommended
//     rollback WITHOUT writing the file (PR F).
const (
	RemediationNone            = "none"
	RemediationExpand          = "expand"
	RemediationRollback        = "rollback"
	RemediationRecommendedOnly = "recommended_only"
)

// Stable-default rollout stage. A rollback resets current_controller_count to
// this variant (value 0), pulling all controllers back to the stable backend.
// Mirrors actions.StageNoneVariant / actions.StageNoneValue, redeclared here so
// the decision package needs no dependency on actions (actions imports decision,
// not the other way around). The actuator's StageVariantForValue(0) would also
// yield this, but the rollback target is a fixed, named constant.
const (
	rolloutStageNoneVariant = "none"
	rolloutStageNoneValue   = 0
)

// DefaultRolloutCooldown is how long re-expansion is suppressed after a
// rollback, so fitness has time to recover and stabilize at the reset stage
// before the loop starts climbing the ladder again. Without it the loop would
// roll back, immediately see passing fitness at stage none, and re-expand into
// the same failure. Tunable via AGENT_ROLLOUT_COOLDOWN_SECONDS (read at
// construction in main.go); held in-memory. An agent restart clears it, and so
// does an operator ending or resetting the rollout (#1384).
const DefaultRolloutCooldown = 30 * time.Second

// AttrRolloutControllerCount is the new rollout stage VALUE recorded on the
// decision span when the loop expands (e.g. 3 after advancing none→one→three).
const AttrRolloutControllerCount = "rollout.controller_count"

// rolloutStrategyImmediate is the controller-manager strategy under which ALL
// controllers are routed to the target backend at once (no progressive cohort).
// The current_controller_count ladder is meaningless under it, so the loop
// observes only — it never climbs the ladder or writes flag flips that would
// correspond to no routing change (#829). It mirrors the "immediate" literal in
// services/controller_manager/multiplexer/rollout_router.py.
const rolloutStrategyImmediate = "immediate"

// InfraLoop runs the progressive-rollout expansion controller (#734). On each
// infra observe cycle it gates on controller freshness, evaluates Bluetooth
// fitness against the live fitness.bluetooth.* thresholds, reads the OBSERVED
// rollout state from the health span, and — when fitness passes, the dwell has
// elapsed, and no hold is in force — expands the rollout one stage up the ladder
// by flipping current_controller_count. Every cycle where the rollout is ACTIVE
// emits an agent.infrastructure.decision span.
//
// Rollback is PR F: when fitness FAILS at an active stage (>0) and
// remediation_allowed is true, the loop resets current_controller_count to
// "none" (rolls the whole rollout back) and starts a cooldown that suppresses
// re-expansion (via the holdExpansion hook) until fitness has had time to
// recover. When remediation_allowed is false it records "recommended_only"
// without writing. The loop is safe for concurrent OnInfraEvaluate calls (the
// trace Export handler may invoke it from multiple goroutines): all loop state is
// guarded by mu.
type InfraLoop struct {
	log         *slog.Logger
	fitness     BluetoothFitnessSource
	rollout     RolloutActuator
	remediation RemediationSource
	tracer      trace.Tracer

	gate     rolloutGate
	dwell    time.Duration
	cooldown time.Duration
	now      func() time.Time

	// holdExpansion is a hook that, when it returns true, suppresses an otherwise
	// eligible expansion. PR F installs the post-rollback cooldown hook here in
	// NewInfraLoop (it reads cooldownUntil under mu). Guarded by mu (read inside
	// the locked decide path). Kept as a settable seam for tests.
	holdExpansion func(now time.Time) bool

	// observeStaleness, when > 0, makes the loop skip acting on an InfraContext
	// snapshot whose UpdatedAt is older than this bound (the observation predates
	// the freshness window). GRACEFUL on a zero/absent UpdatedAt: an unset
	// timestamp (test helpers, an older producer) must NOT block action, so the
	// guard only fires when UpdatedAt is non-zero AND too old. Zero disables it.
	observeStaleness time.Duration

	mu            sync.Mutex
	lastExpansion time.Time
	// lastWritten is the controller-count VALUE the loop last advanced the rollout
	// TO (the stage of the last successful expand, or 0 after a rollback). It makes
	// expansion correctness independent of the dwell-vs-observe-latency race (#828):
	// after an expand the observed RolloutCount lags for the full observe latency, so
	// re-reading NextStage(stale_stage) would re-write the SAME stage (a double-write)
	// if the dwell happens to be shorter than that latency. The loop only expands once
	// the observation has CAUGHT UP (observed stage >= lastWritten); until then the
	// just-written stage is in flight and re-writing it is suppressed. Zero means
	// "nothing written yet", so the first expansion is never blocked.
	lastWritten int
	// cooldownUntil is the wall-clock time until which re-expansion is suppressed
	// after a rollback. Zero = no cooldown. In-memory only; cleared with the rest of
	// the rollout state when the rollout becomes inactive or the served stage is no
	// longer the agent's write (resetRolloutStateLocked, #1384).
	cooldownUntil time.Time
	// rollbackInFlight is true between writing a rollback and observing the
	// rollout count return to 0. While set, further failing cycles record "none"
	// (the reset is already in flight) and do NOT re-write. Cleared when the
	// observed RolloutCount reaches 0 — the failure episode is over.
	rollbackInFlight bool
	// servedEpoch is the flag service's boot epoch the last time the loop checked
	// its state against the served stage ("" = not checked since the last write).
	servedEpoch string

	// Dry-run pacing (rollout_enabled off). A dry run writes nothing, so it must
	// NOT touch lastWritten / lastExpansion / rollbackInFlight / cooldownUntil:
	// the observed stage never moves, so that state would never be reconciled and
	// a later real expand or rollback would be blocked until restart (UC-045 A4).
	// These two fields only pace the rehearsed decisions so a long dry run records
	// one would-be expansion per dwell and one would-be rollback per cooldown.
	dryRunLastExpansion time.Time
	dryRunCooldownUntil time.Time
}

// NewInfraLoop builds the progressive-rollout loop (#734). log nil → slog.Default.
// fitness nil → flagd-schema defaults each cycle. dwell ≤ 0 → DefaultRolloutDwell.
//
// rollout may be nil, in which case the loop NEVER expands (every active-rollout
// cycle records remediation.action="none"); it still gates, evaluates fitness,
// and emits the observe span. main.go always passes a GatedRolloutActuator (#1213):
// it dry-runs (real ladder, no-op write) whenever the LIVE rollout_enabled flag is
// off or flagd is unreachable, so the disabled path is recorded as
// decided-but-not-applied rather than not-decided.
func NewInfraLoop(log *slog.Logger, dwell time.Duration, fitness BluetoothFitnessSource, rollout RolloutActuator) *InfraLoop {
	if log == nil {
		log = slog.Default()
	}
	if dwell <= 0 {
		dwell = DefaultRolloutDwell
	}
	l := &InfraLoop{
		log:     log,
		fitness: fitness,
		rollout: rollout,
		// Fail-closed default: until main.go injects the real RemediationReader,
		// rollback is recommend-only (never auto-writes).
		remediation: staticRemediation(false),
		tracer:      otel.Tracer(instrumentationName),
		gate:        defaultRolloutGate,
		dwell:       dwell,
		cooldown:    DefaultRolloutCooldown,
		now:         time.Now,
	}
	// Wire the post-rollback cooldown into the expansion hold seam. The hook reads
	// cooldownUntil, which the failing branch sets after a rollback. shouldExpandLocked
	// already holds mu when it calls this, so read cooldownUntil directly.
	l.holdExpansion = func(now time.Time) bool {
		return !l.cooldownUntil.IsZero() && now.Before(l.cooldownUntil)
	}
	return l
}

// SetRemediationSource injects the `remediation_allowed` gate (main.go passes
// actions.RemediationReader). nil leaves the fail-closed default (recommend-only).
func (l *InfraLoop) SetRemediationSource(src RemediationSource) {
	if src == nil {
		return
	}
	l.mu.Lock()
	l.remediation = src
	l.mu.Unlock()
}

// SetCooldown overrides the post-rollback cooldown (main.go reads
// AGENT_ROLLOUT_COOLDOWN_SECONDS). d ≤ 0 is ignored (keeps the default).
func (l *InfraLoop) SetCooldown(d time.Duration) {
	if d <= 0 {
		return
	}
	l.mu.Lock()
	l.cooldown = d
	l.mu.Unlock()
}

// defaultRolloutGate returns true when ≥1 controller has a fresh health update.
// main.go injects the real gate.ShouldEvaluateInfra at construction; this
// fallback keeps a default-constructed loop from gating everything out. The TTL
// matches the infra controller retention default (5s ≈ five 1Hz windows).
func defaultRolloutGate(snap infracontext.InfraContext, now time.Time) bool {
	const ttl = 5 * time.Second
	for _, c := range snap.Controllers {
		if c != nil && now.Sub(c.LastUpdate) <= ttl {
			return true
		}
	}
	return false
}

// SetGate overrides the freshness gate (main.go injects gate.ShouldEvaluateInfra).
func (l *InfraLoop) SetGate(g func(snap infracontext.InfraContext, now time.Time) bool) {
	if g != nil {
		l.gate = g
	}
}

// SetObserveStaleness sets the freshness bound (#828): an InfraContext snapshot
// whose UpdatedAt is older than d is skipped (the observation predates the
// freshness window). d <= 0 disables the guard (the default). The guard is always
// graceful on a zero/absent UpdatedAt — an unset timestamp never blocks action.
func (l *InfraLoop) SetObserveStaleness(d time.Duration) {
	l.mu.Lock()
	l.observeStaleness = d
	l.mu.Unlock()
}

// OnInfraEvaluate runs one rollout-expansion decision cycle. See InfraLoop's doc
// for the decision matrix. Safe for concurrent use.
func (l *InfraLoop) OnInfraEvaluate(ctx context.Context, snap infracontext.InfraContext) {
	now := l.now()

	// (1) Gate: no fresh controllers → nothing to observe, no span (PR G may
	// revisit emitting an "idle" span).
	if l.gate != nil && !l.gate(snap, now) {
		return
	}

	// (1b) Freshness guard (#828): skip acting on an observation older than the
	// configured bound. GRACEFUL on an absent timestamp — a zero UpdatedAt (test
	// helpers default it to zero; an older producer may omit it) must NOT block
	// action, so the guard only fires when UpdatedAt is non-zero AND too old.
	if l.observeStaleness > 0 && !snap.UpdatedAt.IsZero() && now.Sub(snap.UpdatedAt) > l.observeStaleness {
		return
	}

	// (2) Fitness against the live thresholds.
	th := DefaultBluetoothThresholds()
	if l.fitness != nil {
		th = l.fitness.BluetoothThresholds()
	}
	fit := EvaluateInfraFitness(snap, th)

	// (3) Observed rollout state from the health span. The rollout is ACTIVE only
	// when the controller-manager reports a target backend ("" means strategy off).
	target := snap.Window.TargetBackend
	stage := snap.Window.RolloutCount
	if target == "" {
		// Rollout inactive → no decision span. An operator ended the rollout (strategy
		// off), so forget everything kept about it: the next active rollout starts
		// fresh instead of inheriting a last written stage or a cooldown (#1384).
		l.mu.Lock()
		l.resetRolloutStateLocked("rollout_inactive")
		l.mu.Unlock()
		return
	}

	// Whether this cycle is a rehearsal (rollout_enabled off). Read before taking
	// mu: GatedRolloutActuator resolves a flag here. Decisions are paced against
	// the dry-run fields when true; the write result (applyLocked) is what decides
	// which state is committed, so a gate flip mid-cycle cannot commit a write that
	// never landed. Every active-rollout span carries it, so it is read each cycle.
	dryRun := l.rollout == nil || l.rollout.DryRun()

	// (4) Decide.
	l.mu.Lock()
	action := RemediationNone
	var expandedTo int
	wroteVariant := ""
	var writeErr error

	// The flag service, not this process, owns the stage: if what it serves is no
	// longer the agent's last write (an operator reset, a flag-service restart),
	// drop the in-memory state that would otherwise block expansion (#1384).
	l.reconcileServedLocked(now, stage)

	// Episode bookkeeping: once a rollback's reset has been observed to land
	// (RolloutCount back to 0), the failure episode is over — clear the in-flight
	// flag so a later failure can trigger a fresh rollback.
	if l.rollbackInFlight && stage == rolloutStageNoneValue {
		l.rollbackInFlight = false
	}

	// Under strategy=immediate the controller-manager routes ALL controllers to
	// the target at once, so the count ladder is meaningless: climbing it would
	// only write flag flips and emit "expand" decisions that correspond to no
	// routing change (#829). Observe only — record action="none" and skip the
	// expand/rollback decision entirely. Absent/empty strategy (older
	// controller-manager spans) is NOT immediate, so it falls through to the
	// progressive ladder unchanged — no regression.
	immediate := snap.Window.RolloutStrategy == rolloutStrategyImmediate

	switch {
	case immediate:
		// Observe-only: action remains RemediationNone, no write, no ladder climb.

	case fit.Evaluated && fit.Passing:
		// Fitness passes → climb the ladder if eligible (PR E expansion).
		if l.rollout != nil {
			if variant, value, ok := l.rollout.NextStage(stage); ok && l.shouldExpandLocked(now, stage, dryRun) {
				action = RemediationExpand
				expandedTo = value
				wroteVariant = variant
				dryRun, writeErr = l.applyLocked(variant, dryRun)
				switch {
				case writeErr != nil:
					// nothing committed: the next eligible cycle retries
				case dryRun:
					l.dryRunLastExpansion = now
				default:
					l.lastExpansion = now
					l.lastWritten = value
					l.servedEpoch = ""
				}
			}
		}

	case fit.Evaluated && !fit.Passing:
		// Fitness FAILS → remediation (PR F). Three sub-cases:
		switch {
		case stage <= rolloutStageNoneValue:
			// Nothing to roll back: already at (or below) the stable default. Record
			// "none" with the violations; no write.
			action = RemediationNone
		case l.rollbackInFlight:
			// A rollback is already in flight (written, count not yet back to 0).
			// Re-emitting "rollback" would lie (no write this cycle), so record "none"
			// with the violations while the controller-manager catches up.
			action = RemediationNone
		case dryRun && now.Before(l.dryRunCooldownUntil):
			// A rehearsed rollback was recorded within the last cooldown. Record
			// "none" instead of re-rehearsing every cycle.
			action = RemediationNone
		case l.remediation != nil && l.remediation.RemediationAllowed() && l.rollout != nil:
			// Allowed + active stage → roll the whole rollout back to "none".
			// target_backend is deliberately left untouched (operator intent for the
			// next attempt). Start the post-rollback cooldown so the loop does not
			// immediately re-expand into the same failure once fitness recovers.
			action = RemediationRollback
			wroteVariant = rolloutStageNoneVariant
			dryRun, writeErr = l.applyLocked(rolloutStageNoneVariant, dryRun)
			switch {
			case writeErr != nil:
				// nothing committed: the next failing cycle retries
			case dryRun:
				// Rehearsal: nothing was written, so the stage will not return to 0.
				// Do not mark it in flight or start the real cooldown (UC-045 A4).
				l.dryRunCooldownUntil = now.Add(l.cooldown)
			default:
				l.rollbackInFlight = true
				l.cooldownUntil = now.Add(l.cooldown)
				// Reset the reconcile anchor: the rollout was reset to none(0), so the
				// next expansion is gated on the observation returning to 0 (#828).
				l.lastWritten = rolloutStageNoneValue
				l.servedEpoch = ""
			}
		default:
			// Active stage but rollback NOT allowed → recommend only (span, no write).
			action = RemediationRecommendedOnly
		}
	}
	l.mu.Unlock()

	// (5) Span: every ACTIVE-rollout cycle is audited.
	l.emitDecisionSpan(ctx, now, snap, fit, target, stage, action, expandedTo, wroteVariant, dryRun, writeErr)
}

// holdsBlockingStateLocked reports whether in-memory rollout state would hold
// back the loop this cycle: an expansion the observation has not caught up with,
// a rollback in flight, or a running post-rollback cooldown. Only then is the
// served stage worth a read. Caller holds mu.
func (l *InfraLoop) holdsBlockingStateLocked(now time.Time, observedStage int) bool {
	return observedStage < l.lastWritten || l.rollbackInFlight ||
		(!l.cooldownUntil.IsZero() && now.Before(l.cooldownUntil))
}

// reconcileServedLocked checks the loop's state against the stage the flag
// service serves and resets it when the two disagree (#1384):
//
//   - a new boot epoch: the flag service restarted and lost the agent's write;
//   - the served value is not the agent's own overlay, is off the ladder, or is
//     not the value the agent last wrote: someone else (an operator, the stage
//     scripts) moved or reset the stage.
//
// It reads only while the state blocks something (holdsBlockingStateLocked) and
// only through an actuator that implements RolloutStageReader. A read error
// keeps the state (fail safe: an unreachable flag service never unblocks an
// expansion). Caller holds mu.
func (l *InfraLoop) reconcileServedLocked(now time.Time, observedStage int) {
	reader, ok := l.rollout.(RolloutStageReader)
	if !ok || !l.holdsBlockingStateLocked(now, observedStage) {
		return
	}
	served, err := reader.ServedStage()
	if err != nil {
		l.log.Debug("agent.rollout_served_stage_unreadable", "error", err)
		return
	}
	switch {
	case l.servedEpoch != "" && served.Epoch != "" && served.Epoch != l.servedEpoch:
		l.resetRolloutStateLocked("flag_service_restarted")
	case !served.ByAgent || !served.OnLadder || served.Value != l.lastWritten:
		l.resetRolloutStateLocked("stage_changed_externally")
	default:
		l.servedEpoch = served.Epoch
	}
}

// resetRolloutStateLocked forgets everything the loop keeps about the current
// rollout, real and rehearsed, so the next active rollout starts as after an
// agent restart. A no-op (and silent) when there is nothing to forget. Caller
// holds mu.
func (l *InfraLoop) resetRolloutStateLocked(reason string) {
	if l.lastWritten == 0 && l.lastExpansion.IsZero() && !l.rollbackInFlight &&
		l.cooldownUntil.IsZero() && l.servedEpoch == "" &&
		l.dryRunLastExpansion.IsZero() && l.dryRunCooldownUntil.IsZero() {
		return
	}
	l.log.Info("agent.rollout_state_reset", "reason", reason,
		"last_written", l.lastWritten, "rollback_in_flight", l.rollbackInFlight)
	l.lastWritten = rolloutStageNoneValue
	l.lastExpansion = time.Time{}
	l.rollbackInFlight = false
	l.cooldownUntil = time.Time{}
	l.servedEpoch = ""
	l.dryRunLastExpansion = time.Time{}
	l.dryRunCooldownUntil = time.Time{}
}

// applyLocked writes variant through the actuator and reports whether this cycle
// was a dry run (nothing reached the flag service). An actuator implementing
// RolloutApplier answers from the same gate read as the write; any other actuator
// is taken at its pre-cycle DryRun() answer. Caller holds mu.
func (l *InfraLoop) applyLocked(variant string, dryRun bool) (bool, error) {
	if a, ok := l.rollout.(RolloutApplier); ok {
		applied, err := a.ApplyControllerCount(variant)
		return !applied, err
	}
	return dryRun, l.rollout.SetControllerCount(variant)
}

// shouldExpandLocked is the expansion guard: the observation must have caught up
// to the last write, the dwell since the last expansion must have elapsed, AND no
// hold may be in force. Caller holds mu; observedStage is the rollout count read
// off this cycle's health span.
//
//   - reconcile (#828): the observed stage must be >= the value we last wrote
//     (lastWritten). After an expand the observed RolloutCount lags for the full
//     observe latency, so re-reading NextStage(stale_stage) would re-write the
//     SAME stage if the dwell is shorter than that latency. Suppressing expansion
//     until the observation catches up makes correctness independent of the
//     dwell-vs-latency race — it no longer relies on the dwell exceeding latency.
//     lastWritten zero (never written) never blocks the first expansion.
//   - dwell: fitness needs time to observe each new stage before we add more
//     controllers. lastExpansion zero (never expanded) passes the dwell check so
//     the first expansion is not delayed.
//   - holdExpansion: PR F's post-rollback cooldown — true while now is before
//     cooldownUntil, blocking re-expansion for a window after a rollback.
//
// In a dry run (dryRun=true) the reconcile and hold checks still apply, since a
// real write may be in flight from before the gate went off, and the dwell is
// measured from the later of the last real and the last rehearsed expansion. A
// rehearsed rollback's cooldown holds rehearsed AND real expansions (see below).
func (l *InfraLoop) shouldExpandLocked(now time.Time, observedStage int, dryRun bool) bool {
	if observedStage < l.lastWritten {
		// The observation has not yet caught up to the stage we already wrote — the
		// last expansion is still in flight. Acting now would re-write the same stage.
		return false
	}
	last := l.lastExpansion
	if dryRun && l.dryRunLastExpansion.After(last) {
		last = l.dryRunLastExpansion
	}
	if !last.IsZero() && now.Sub(last) < l.dwell {
		return false
	}
	if l.holdExpansion != nil && l.holdExpansion(now) {
		return false
	}
	// A rehearsed rollback's cooldown holds expansion in both modes. Holding a
	// REAL expansion right after rollout_enabled is turned back on is the
	// conservative choice: the rehearsal said the stage was unhealthy, and the
	// cooldown only ever delays an expansion, never a rollback.
	if now.Before(l.dryRunCooldownUntil) {
		return false
	}
	return true
}

// emitDecisionSpan emits the agent.infrastructure.decision span for one
// ACTIVE-rollout cycle, mirroring telemetry.go conventions (tracer.Start +
// WithAttributes, error.type + status on write failure). expandedTo is only
// meaningful when action == RemediationExpand; wroteVariant is set for both
// expand (next variant) and rollback ("none").
func (l *InfraLoop) emitDecisionSpan(
	ctx context.Context,
	now time.Time,
	snap infracontext.InfraContext,
	fit InfraFitnessResult,
	target string,
	stage int,
	action string,
	expandedTo int,
	wroteVariant string,
	dryRun bool,
	writeErr error,
) {
	// Single builder: EVERY emission path lands the full attribute schema (#737).
	// dry-run is true when the actuator never applies writes (the rehearsal
	// actuator) or is absent entirely — either way no flip reached the flag service,
	// so the span must say so (M3 audit: rehearsed decisions were indistinguishable
	// from real ones).
	attrs := infraDecisionAttributes(infraDecisionAttrs{
		snap:       snap,
		fit:        fit,
		target:     target,
		action:     action,
		expandedTo: expandedTo,
		dryRun:     dryRun,
	})

	_, span := l.tracer.Start(ctx, SpanInfraDecision,
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(attrs...),
	)
	defer span.End()

	if writeErr != nil {
		span.RecordError(writeErr)
		span.SetStatus(codes.Error, writeErr.Error())
		event := "agent.rollout_expand_failed"
		if action == RemediationRollback {
			event = "agent.rollout_rollback_failed"
		}
		l.log.Error(event,
			"target", target, "from_stage", stage, "to_variant", wroteVariant, "error", writeErr)
		return
	}

	switch action {
	case RemediationExpand:
		l.log.Info("agent.rollout_expand",
			"target", target, "from_stage", stage, "to_stage", expandedTo,
			"to_variant", wroteVariant, "dry_run", dryRun)
	case RemediationRollback:
		l.log.Warn("agent.rollout_rollback",
			"target", target, "from_stage", stage, "to_variant", wroteVariant,
			"violations", fit.ViolationsString(), "dry_run", dryRun)
	case RemediationRecommendedOnly:
		l.log.Warn("agent.rollout_rollback_recommended",
			"target", target, "stage", stage, "violations", fit.ViolationsString(),
			"reason", "remediation_allowed=false")
	}
}

// SetHoldExpansion OVERRIDES the expansion-hold hook. NewInfraLoop already
// installs the post-rollback cooldown hook (reads cooldownUntil); this seam lets
// tests substitute a deterministic hold (or nil to disable holding entirely).
// When hold returns true an otherwise-eligible expansion is suppressed (the loop
// still emits an observe span). Safe to call before the loop is driven.
func (l *InfraLoop) SetHoldExpansion(hold func(now time.Time) bool) {
	l.mu.Lock()
	l.holdExpansion = hold
	l.mu.Unlock()
}
