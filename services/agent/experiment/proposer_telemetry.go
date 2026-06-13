package experiment

// proposer_telemetry.go adds the M7-4 (#931) PROPOSE-side span schema, extending
// the existing code_improvement.* trace family (telemetry.go) rather than
// inventing a new one. Two spans exist in the proposal pipeline:
//
//   - SpanProposeAttempt (THIS file): one per Proposer.ProposeOnce cycle — wraps
//     the inference + decode + Gate submission. Carries the engine, the model's
//     reasoning (the AC's code_improvement.proposed_diff_summary), and the cycle
//     OUTCOME (proposed/gated/no_backend/undecodable/blocked). It is the audit of
//     "the agent tried to propose; here is what happened end to end".
//   - SpanProposed (telemetry.go, emitted by Gate.Review): the existing per-write
//     span carrying blocked/reason. SpanProposeAttempt is the PARENT span; the
//     Gate's SpanProposed nests under it on an accepted/blocked decode, so a Jaeger
//     trace shows the whole propose→gate flow.
//
// Kept in a separate file from telemetry.go so the merged Gate/Writer ownership
// (gate.go/proposal.go/validate.go are owned elsewhere) is untouched; the consts
// reuse the AttrFlagKey/AttrRationale/AttrReason keys from telemetry.go so the two
// spans share one attribute vocabulary.
const (
	// SpanProposeAttempt wraps one Proposer.ProposeOnce cycle (#931, M7-4). The
	// Gate's SpanProposed nests under it.
	SpanProposeAttempt = "code_improvement.propose_attempt"
)

const (
	// AttrEngine is the resolved agent.code_improvement.engine identifier (#931 AC
	// "engine backend is flag-selectable"). Attribution only — the backend
	// selection happens in main.go.
	AttrEngine = "code_improvement.engine"
	// AttrProposedDiffSummary records the structured experiment the model proposed
	// (#931 AC "proposed diff summary + full reasoning visible as a span"). Since
	// the rescope has no source diff, this is the compact "set flag=value for
	// shadow games" summary plus the rationale.
	AttrProposedDiffSummary = "code_improvement.proposed_diff_summary"
	// NOTE: the cycle outcome rides on the EXISTING AttrOutcome key
	// ("code_improvement.outcome", defined in validate_telemetry.go) — the propose
	// and validate stages share one outcome attribute vocabulary so a dashboard can
	// enumerate every code_improvement.* cycle result on one key. The propose-side
	// values are the ProposeOutcome* constants (proposed/gated/no_backend/
	// undecodable/blocked); the validate-side values are the Outcome* constants
	// (promote/discard/revert). The span NAME distinguishes the stage.
)
