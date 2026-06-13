package experiment

// Span names + attribute keys for the M7-7 synthetic VALIDATION cycle (#935).
//
// M7-7 sits BETWEEN the agent forming a Proposal and that proposal becoming the
// real default (#936). Its job: run the proposed flag experiment through the M6
// synthetic/shadow game suite (#774–#893), measure fitness against a baseline
// drawn from recent real games (#731 fitness eval), and DECIDE — promote,
// discard, or revert — purely on the fitness delta.
//
// The full self-improvement cycle is ONE trace tree (the #935 AC, "single trace
// tree"): the caller's evaluation → invocation spans parent a
// code_improvement.synthetic_validation span (this package), which in turn
// parents either code_improvement.apply (PROMOTE) or code_improvement.discard
// (DISCARD/REVERT). Dot notation matches the repo span convention
// (decision/telemetry.go). These consts live in a SEPARATE file from telemetry.go
// (which a parallel PR owns) so the additions never collide; the experiment.Attr*
// keys there (flag_key/blocked/reason/rationale) describe the WRITE gate, the
// keys here describe the VALIDATE gate — distinct phases of the same #931/#932
// flag-experiment track.
const (
	// SpanSyntheticValidation wraps one Validator.Validate of a Proposal: run the
	// synthetic suite with the experiment active, measure before/after fitness,
	// decide. It is the issue's "synthetic_validation" node in the
	// evaluation → invocation → synthetic_validation → apply|discard tree.
	SpanSyntheticValidation = "code_improvement.synthetic_validation"
	// SpanApply is the PROMOTE leaf: the experiment improved fitness past the
	// threshold, so the cycle resolves to apply. NOTE M7-7 does NOT itself change
	// the real defaultVariant — that gated promotion is M7-8's (#936) job; here a
	// PROMOTE outcome means "this experiment earned promotion", recorded so the
	// trace shows the decision even though the real-default move happens later.
	SpanApply = "code_improvement.apply"
	// SpanDiscard is the DISCARD/REVERT leaf: the experiment did not clear the
	// threshold (DISCARD) or actively degraded fitness with revert_on_degradation
	// on (REVERT). Either way nothing is promoted; the leaf records which.
	SpanDiscard = "code_improvement.discard"
)

// Attribute keys of the synthetic-validation span schema (#935). The #935 AC
// requires fitness_before/fitness_after on the span and the cycle visible as one
// trace; these carry them.
const (
	// AttrFitnessBefore is the BASELINE fitness, drawn from the last N real games
	// (the injected Baseline source). Recorded on the validation span so a Jaeger
	// trace shows what the experiment was measured against. (#935 AC: "fitness_before
	// recorded on the span".)
	AttrFitnessBefore = "code_improvement.fitness_before"
	// AttrFitnessAfter is the fitness measured AFTER running the synthetic suite
	// with the experiment active (the injected FitnessMeasurer over the
	// SyntheticRunner's result). (#935 AC: "fitness_after recorded on the span".)
	AttrFitnessAfter = "code_improvement.fitness_after"
	// AttrFitnessDelta is fitness_after - fitness_before: the improvement the
	// promote/discard decision keys on. Recorded alongside before/after so the
	// decision is self-explaining in the trace (no client-side subtraction needed).
	AttrFitnessDelta = "code_improvement.fitness_delta"
	// AttrValidationGames is the COUNT of synthetic games actually run this cycle
	// (the resolved code_improvement.validation_games flag value). On the span so
	// the trace shows the sample size the fitness_after rests on.
	AttrValidationGames = "code_improvement.validation_games"
	// AttrImprovementThreshold is the resolved
	// code_improvement.fitness_improvement_threshold the delta was compared
	// against — recorded so the trace shows the bar the experiment had to clear.
	AttrImprovementThreshold = "code_improvement.fitness_improvement_threshold"
	// AttrOutcome is the cycle's decision, one of the Outcome* string constants
	// (promote/discard/revert). The single attribute a dashboard groups by to see
	// "what did the agent decide for this experiment". (#935 AC: the apply|discard
	// branch of the trace.)
	AttrOutcome = "code_improvement.outcome"
	// AttrReverted is true ONLY on a REVERT outcome: the experiment degraded
	// fitness and revert_on_degradation was set, so the active experiment was
	// rolled back. Absent/false on PROMOTE and plain DISCARD. (#935 AC: "revert on
	// degradation when revert_on_degradation = true".)
	AttrReverted = "code_improvement.reverted"
	// AttrValidationError carries the typed reason the cycle could not complete
	// (baseline unavailable, synthetic run failed, measurement failed). Present
	// only on the error path; its presence means the cycle FAILED CLOSED — no
	// promotion, outcome=discard — never silently promoted on a broken signal.
	AttrValidationError = "code_improvement.validation_error"
)
