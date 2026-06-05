package decision

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
