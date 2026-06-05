package decision

import (
	"go.opentelemetry.io/otel/attribute"

	"github.com/joustmania/agent/infracontext"
)

// Infrastructure-domain decision telemetry constants (#733, M3). The agent runs
// a parallel OBSERVE path over the controller.bluetooth_health span: it tracks
// Bluetooth transport health and (in later stacked PRs D/E/F) evaluates fitness
// and emits a remediation decision span.
//
// This file declares the span name + attribute keys ONLY — NO span is emitted in
// this PR. They live here, alongside the game decision schema (telemetry.go), so
// PR D/E/F import a single converged vocabulary rather than redefining keys.
const (
	// SpanInfraDecision is the infrastructure remediation decision span, the
	// infra-domain parallel to SpanDecision (agent.decision). Emitted by PR E/F.
	SpanInfraDecision = "agent.infrastructure.decision"
)

// Custom attribute keys of the infrastructure-decision span schema. The rollout
// and fitness/remediation keys are the infra-domain decision attribution; the
// bluetooth.* keys mirror the controller.bluetooth_health span contract so the
// signals that drove a decision can be lifted onto its span verbatim.
const (
	// AttrRolloutTarget is the rollout target adapter_type in effect
	// (bluetooth.target_backend), "" when rollout is off.
	AttrRolloutTarget = "rollout.target"
	// AttrFitnessPassing is whether the infrastructure fitness check passed (#733
	// fitness lands in PR D).
	AttrFitnessPassing = "fitness.passing"
	// AttrFitnessViolations lists the failing fitness checks for this decision.
	AttrFitnessViolations = "fitness.violations"
	// AttrRemediationAction is the remediation the decision selected (e.g. roll
	// back the rollout); the infra-domain parallel to AttrDecisionAction.
	AttrRemediationAction = "remediation.action"
	// AttrRolloutDryRun is whether the actuator only REHEARSED the decision (true)
	// rather than applying it to rollout.json (false). True for the dry-run
	// actuator (AGENT_ROLLOUT_ENABLED=false, the compose default) and for a nil
	// actuator (the loop never writes); false for the real RolloutWriter. Present
	// on EVERY decision span so a Jaeger consumer can tell a rehearsed
	// expand/rollback apart from an applied one — they were otherwise identical.
	AttrRolloutDryRun = "rollout.dry_run"

	// Bluetooth signal attribute keys (controller.bluetooth_health contract).
	// Re-declared here, decoupled from the infracontext extractor constants, so
	// the decision package has no dependency on infracontext for span emission.
	AttrBluetoothEventGapMs        = "bluetooth.event_gap_ms"
	AttrBluetoothDroppedEventsPct  = "bluetooth.dropped_events_pct"
	AttrBluetoothMovementUpdateHz  = "bluetooth.movement_update_hz"
	AttrBluetoothActiveControllers = "bluetooth.active_controllers"
	AttrBluetoothTargetBackend     = "bluetooth.target_backend"
	AttrBluetoothRolloutCount      = "bluetooth.rollout_count"
)

// infraDecisionAttrs is the input bundle for the single span-attribute builder.
// Bundling the inputs (rather than a long parameter list) keeps the call site in
// emitDecisionSpan a one-liner and makes the "every path goes through the
// builder" invariant easy to audit: there is exactly ONE producer of
// agent.infrastructure.decision attributes.
type infraDecisionAttrs struct {
	// snap is the observed Bluetooth-health window + controllers that drove the
	// decision; its window signals are lifted onto the span verbatim.
	snap infracontext.InfraContext
	// fit is the fitness verdict for this cycle (passing + violations string).
	fit InfraFitnessResult
	// target is the rollout target adapter in effect (rollout.target).
	target string
	// action is the remediation the loop selected (one of the Remediation*
	// constants).
	action string
	// expandedTo is the rollout stage VALUE the loop expanded to; only meaningful
	// (and only recorded) when action == RemediationExpand.
	expandedTo int
	// dryRun is whether the actuator only rehearsed this decision (no write to
	// rollout.json) rather than applying it. Recorded on every span.
	dryRun bool
}

// infraDecisionAttributes is the SOLE builder of the agent.infrastructure.decision
// span attribute set (#737). Every span-emission path in the loop must route
// through it, so the schema is guaranteed complete and consistent.
//
// The contract — enforced by the schema-completeness test — is that EVERY emitted
// decision span carries ALL FIVE core attributes, present (not absent) on every
// outcome:
//
//   - rollout.target        — the rollout target adapter (always)
//   - fitness.passing       — bool, always
//   - fitness.violations    — the violation string; the EMPTY string when passing
//     (present-but-empty, never absent, so a query can distinguish "passing" from
//     "attribute missing")
//   - remediation.action    — the selected action (always)
//   - rollout.dry_run       — bool, always: whether the actuator only rehearsed
//     this decision (no write) rather than applying it, so a Jaeger consumer can
//     tell a rehearsed expand/rollback apart from a real one
//
// Plus the observed bluetooth.* window signals (target_backend + rollout_count
// always; the four optional float/int signals only when present in the window so
// the span never fabricates a reading), and rollout.controller_count only on an
// expand (the stage the loop advanced to).
func infraDecisionAttributes(in infraDecisionAttrs) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		// The five core attributes — unconditional on every path.
		attribute.String(AttrRolloutTarget, in.target),
		attribute.Bool(AttrFitnessPassing, in.fit.Evaluated && in.fit.Passing),
		attribute.String(AttrFitnessViolations, in.fit.ViolationsString()),
		attribute.String(AttrRemediationAction, in.action),
		attribute.Bool(AttrRolloutDryRun, in.dryRun),
		// Window context that always exists on an active-rollout cycle.
		attribute.String(AttrBluetoothTargetBackend, in.snap.Window.TargetBackend),
		attribute.Int(AttrBluetoothRolloutCount, in.snap.Window.RolloutCount),
	}
	// Optional window signals: recorded only when observed (a missing signal
	// contributes no attribute rather than a fabricated zero).
	if v := in.snap.Window.EventGapMs; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothEventGapMs, *v))
	}
	if v := in.snap.Window.DroppedEventsPct; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothDroppedEventsPct, *v))
	}
	if v := in.snap.Window.MovementUpdateHz; v != nil {
		attrs = append(attrs, attribute.Float64(AttrBluetoothMovementUpdateHz, *v))
	}
	if v := in.snap.Window.ActiveControllers; v != nil {
		attrs = append(attrs, attribute.Int(AttrBluetoothActiveControllers, *v))
	}
	// The stage the loop expanded to (the WOULD-be stage in dry-run); only an
	// expand has a meaningful destination.
	if in.action == RemediationExpand {
		attrs = append(attrs, attribute.Int(AttrRolloutControllerCount, in.expandedTo))
	}
	return attrs
}
