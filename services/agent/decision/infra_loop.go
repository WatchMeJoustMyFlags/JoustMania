package decision

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
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
// construction in main.go); held in-memory, so an agent restart clears it.
const DefaultRolloutCooldown = 30 * time.Second

// AttrRolloutControllerCount is the new rollout stage VALUE recorded on the
// decision span when the loop expands (e.g. 3 after advancing none→one→three).
const AttrRolloutControllerCount = "rollout.controller_count"

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

	mu            sync.Mutex
	lastExpansion time.Time
	// cooldownUntil is the wall-clock time until which re-expansion is suppressed
	// after a rollback. Zero = no cooldown. In-memory only (process restart clears
	// it, which is the demo reset path).
	cooldownUntil time.Time
	// rollbackInFlight is true between writing a rollback and observing the
	// rollout count return to 0. While set, further failing cycles record "none"
	// (the reset is already in flight) and do NOT re-write. Cleared when the
	// observed RolloutCount reaches 0 — the failure episode is over.
	rollbackInFlight bool
}

// NewInfraLoop builds the progressive-rollout loop (#734). log nil → slog.Default.
// fitness nil → flagd-schema defaults each cycle. dwell ≤ 0 → DefaultRolloutDwell.
//
// rollout may be nil, in which case the loop NEVER expands (every active-rollout
// cycle records remediation.action="none"); it still gates, evaluates fitness,
// and emits the observe span. When AGENT_ROLLOUT_ENABLED is off, main.go passes a
// dry-run actuator (real ladder, no-op write) instead, so the disabled path is
// recorded as decided-but-not-applied rather than not-decided.
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

// OnInfraEvaluate runs one rollout-expansion decision cycle. See InfraLoop's doc
// for the decision matrix. Safe for concurrent use.
func (l *InfraLoop) OnInfraEvaluate(ctx context.Context, snap infracontext.InfraContext) {
	now := l.now()

	// (1) Gate: no fresh controllers → nothing to observe, no span (PR G may
	// revisit emitting an "idle" span).
	if l.gate != nil && !l.gate(snap, now) {
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
		// Rollout inactive → no decision span, nothing to do.
		return
	}

	// (4) Decide.
	l.mu.Lock()
	action := RemediationNone
	var expandedTo int
	wroteVariant := ""
	var writeErr error

	// Episode bookkeeping: once a rollback's reset has been observed to land
	// (RolloutCount back to 0), the failure episode is over — clear the in-flight
	// flag so a later failure can trigger a fresh rollback.
	if l.rollbackInFlight && stage == rolloutStageNoneValue {
		l.rollbackInFlight = false
	}

	switch {
	case fit.Evaluated && fit.Passing:
		// Fitness passes → climb the ladder if eligible (PR E expansion).
		if l.rollout != nil {
			if variant, value, ok := l.rollout.NextStage(stage); ok && l.shouldExpandLocked(now) {
				action = RemediationExpand
				expandedTo = value
				wroteVariant = variant
				writeErr = l.rollout.SetControllerCount(variant)
				if writeErr == nil {
					l.lastExpansion = now
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
		case l.remediation != nil && l.remediation.RemediationAllowed() && l.rollout != nil:
			// Allowed + active stage → roll the whole rollout back to "none".
			// target_backend is deliberately left untouched (operator intent for the
			// next attempt). Start the post-rollback cooldown so the loop does not
			// immediately re-expand into the same failure once fitness recovers.
			action = RemediationRollback
			wroteVariant = rolloutStageNoneVariant
			writeErr = l.rollout.SetControllerCount(rolloutStageNoneVariant)
			if writeErr == nil {
				l.rollbackInFlight = true
				l.cooldownUntil = now.Add(l.cooldown)
			}
		default:
			// Active stage but rollback NOT allowed → recommend only (span, no write).
			action = RemediationRecommendedOnly
		}
	}
	l.mu.Unlock()

	// (5) Span: every ACTIVE-rollout cycle is audited.
	l.emitDecisionSpan(ctx, now, snap, fit, target, stage, action, expandedTo, wroteVariant, writeErr)
}

// shouldExpandLocked is the expansion guard: the dwell since the last expansion
// must have elapsed AND no hold may be in force. Caller holds mu.
//
//   - dwell: fitness needs time to observe each new stage before we add more
//     controllers. lastExpansion zero (never expanded) passes the dwell check so
//     the first expansion is not delayed.
//   - holdExpansion: PR F's post-rollback cooldown — true while now is before
//     cooldownUntil, blocking re-expansion for a window after a rollback.
func (l *InfraLoop) shouldExpandLocked(now time.Time) bool {
	if !l.lastExpansion.IsZero() && now.Sub(l.lastExpansion) < l.dwell {
		return false
	}
	if l.holdExpansion != nil && l.holdExpansion(now) {
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
	writeErr error,
) {
	attrs := []attribute.KeyValue{
		attribute.String(AttrRolloutTarget, target),
		attribute.Bool(AttrFitnessPassing, fit.Evaluated && fit.Passing),
		attribute.String(AttrFitnessViolations, fit.ViolationsString()),
		attribute.String(AttrRemediationAction, action),
		attribute.String(AttrBluetoothTargetBackend, snap.Window.TargetBackend),
		attribute.Int(AttrBluetoothRolloutCount, snap.Window.RolloutCount),
	}
	if v := snap.Window.EventGapMs; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothEventGapMs, *v))
	}
	if v := snap.Window.DroppedEventsPct; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothDroppedEventsPct, *v))
	}
	if v := snap.Window.MovementUpdateHz; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothMovementUpdateHz, *v))
	}
	if v := snap.Window.ActiveControllers; v != nil {
		attrs = append(attrs, attribute.Int(AttrBluetoothActiveControllers, *v))
	}
	if action == RemediationExpand {
		// The stage the loop expanded to (the WOULD-be stage in DRY-RUN).
		attrs = append(attrs, attribute.Int(AttrRolloutControllerCount, expandedTo))
	}

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
			"to_variant", wroteVariant)
	case RemediationRollback:
		l.log.Warn("agent.rollout_rollback",
			"target", target, "from_stage", stage, "to_variant", wroteVariant,
			"violations", fit.ViolationsString())
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
