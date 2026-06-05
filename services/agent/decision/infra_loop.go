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

// Non-action remediation placeholder for the rollout decision span. In PR E the
// loop OBSERVES — it expands when fitness passes but never rolls back. A
// fitness-failing cycle therefore records remediation.action="none" (observe
// only). PR F introduces the rollback semantics ("rollback" /
// "recommended_only"); keeping this distinct from those values lets the M3 PR G
// narrative test tell an E observe-cycle apart from an F rollback-blocked cycle.
const (
	RemediationNone   = "none"
	RemediationExpand = "expand"
)

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
// Rollback is PR F: this PR's fitness-failing branch only records (no write), and
// the holdExpansion hook (always false here) is where PR F's post-rollback
// cooldown plugs in. The loop is safe for concurrent OnInfraEvaluate calls (the
// trace Export handler may invoke it from multiple goroutines): all loop state is
// guarded by mu.
type InfraLoop struct {
	log     *slog.Logger
	fitness BluetoothFitnessSource
	rollout RolloutActuator
	tracer  trace.Tracer

	gate  rolloutGate
	dwell time.Duration
	now   func() time.Time

	// holdExpansion is a hook that, when it returns true, suppresses an otherwise
	// eligible expansion. It is always nil/false in PR E; PR F sets it to enforce a
	// post-rollback cooldown. Guarded by mu (read inside the locked decide path).
	holdExpansion func(now time.Time) bool

	mu            sync.Mutex
	lastExpansion time.Time
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
	return &InfraLoop{
		log:     log,
		fitness: fitness,
		rollout: rollout,
		tracer:  otel.Tracer(instrumentationName),
		gate:    defaultRolloutGate,
		dwell:   dwell,
		now:     time.Now,
	}
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

	if fit.Evaluated && fit.Passing && l.rollout != nil {
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
	// fitness failing → action stays RemediationNone (observe only; rollback is PR F).
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
//   - holdExpansion: always false in PR E; PR F's post-rollback cooldown sets it
//     to block re-expansion for a window after a rollback.
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
// WithAttributes, error.type + status on write failure). expandedTo/wroteVariant
// are only meaningful when action == RemediationExpand.
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
		l.log.Error("agent.rollout_expand_failed",
			"target", target, "from_stage", stage, "to_variant", wroteVariant, "error", writeErr)
		return
	}

	if action == RemediationExpand {
		l.log.Info("agent.rollout_expand",
			"target", target, "from_stage", stage, "to_stage", expandedTo,
			"to_variant", wroteVariant)
	}
}

// SetHoldExpansion installs the PR F post-rollback cooldown hook. When hold
// returns true an otherwise-eligible expansion is suppressed (the loop still
// emits an observe span). Nil clears the hook (no hold). Safe to call before the
// loop is driven.
func (l *InfraLoop) SetHoldExpansion(hold func(now time.Time) bool) {
	l.mu.Lock()
	l.holdExpansion = hold
	l.mu.Unlock()
}
