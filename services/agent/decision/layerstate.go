package decision

import "github.com/joustmania/agent/flags"

// BlockReason identifies which layer/policy blocked a decision. It is the
// structured attribution issue #729 lifts onto the decision span.
type BlockReason string

const (
	// ReasonNotAllowed: the intervention is not in interventions_allowed
	// (permission layer, allow-list gate from #727).
	ReasonNotAllowed BlockReason = "not_allowed"
	// ReasonBatteryThreshold: the target player's battery is below
	// policy.battery_threshold (permission layer, policy).
	ReasonBatteryThreshold BlockReason = "battery_threshold"
	// ReasonRateLimit: the weighted per-minute budget is exhausted
	// (permission layer, policy.max_interventions_per_minute).
	ReasonRateLimit BlockReason = "rate_limit"
)

// DecisionOutcome records what happened to one candidate decision this cycle:
// the intervention, its target, whether it dispatched, and—if not—why.
type DecisionOutcome struct {
	Intervention string
	TargetSerial string
	// Dispatched is true when the decision passed every gate and reached the
	// action sink.
	Dispatched bool
	// BlockReason is set only when Dispatched is false.
	BlockReason BlockReason
	// Weight is the rate-limit cost charged (or that would have been charged).
	Weight float64
}

// LayerState is the cohesive, span-ready record of everything the four flag
// layers evaluated this cycle, plus the per-decision outcomes. It is the single
// source of truth issue #729 lifts onto the decision span verbatim, so it is
// deliberately flat and self-describing.
//
// Layout (one field group per layer):
//
//	Existence:   Enabled, Mode
//	Objective:   Objectives
//	Capability:  Model, PromptVariant
//	Permission:  InterventionsAllowed + the three Policy* values
//	Outcomes:    per-decision dispatch/block results
type LayerState struct {
	// --- Existence layer ---
	Enabled bool
	Mode    string

	// --- Objective layer ---
	Objectives map[string]float64

	// --- Capability layer (recorded, consumed by M4 LLM path) ---
	Model         string
	PromptVariant string

	// LLMFallbackReason is the inference.fallback_reason this cycle's llm path
	// resolved to (#847 + #741). It is the PER-CYCLE replacement for the static
	// inferenceAttribution. On an llm cycle the loop sets it to, in order of
	// precedence:
	//   - the #847 gate reason (FallbackNotEligible / FallbackInterval /
	//     FallbackBudgetExhausted) when the gate DENIED — resolve_backend never runs;
	//   - the #741 resolve_backend reason when the gate ADMITTED: "" when the
	//     configured tier is reachable, FallbackEndpointUnreachable when a LOWER tier
	//     serves, or FallbackNoBackend when the whole chain is unreachable and rules
	//     decided.
	// layerStateAttributes lifts it onto the decision span. Empty on the rules path
	// (mode!="llm"), where there is no fallback reason at all.
	LLMFallbackReason string

	// InferenceUsed is inference.used this cycle: the tier resolve_backend (#741)
	// determined WOULD serve the decision — claude/copilot via "cloud", "gemma3:4b",
	// "phi4-mini", or InferenceRules ("rules") when the gate denied, the chain was
	// unreachable, or the mode is rules. Defaulted to InferenceRules by newLayerState
	// so a cycle that never resolves a backend (rules mode, gate-denied llm) honestly
	// reports rules. The loop overwrites it with the resolved tier ONLY on a
	// gate-admitted llm cycle. layerStateAttributes emits it verbatim as
	// inference.used, replacing the static inferenceAttribution(mode) value (#847's
	// inference.used was always InferenceRules; #741 makes it honest). Until #739 a
	// "reachable" tier does NOT actually decide — the rules engine still produces the
	// Decision — so this reflects WHO #739 WILL call, not who decided this cycle.
	InferenceUsed string

	// --- Permission layer ---
	InterventionsAllowed      []string
	PolicyBatteryThreshold    int
	PolicyMovementVarianceWin int
	PolicyMaxPerMinute        int

	// --- Fitness layer (#731 hook) ---
	// FitnessEvaluated holds the cycle-level fitness-function results that #731
	// will populate (e.g. session_duration, target_session_seconds). It is the
	// span-attribute hook for fitness.evaluated at the cycle scope: empty/absent
	// until #731 wires the fitness engine, lifted onto the decision span only
	// when non-empty. Per-decision fitness already rides on Decision.Fitness and
	// the per-decision fitness.evaluated attribute; this is the cycle-wide view.
	FitnessEvaluated map[string]float64

	// --- Per-decision outcomes ---
	Candidates int
	Dispatched int
	Blocked    int
	Outcomes   []DecisionOutcome
}

// newLayerState seeds a LayerState from the evaluated flag snapshot. Outcomes
// are filled in as decisions are processed.
func newLayerState(s flags.Snapshot) LayerState {
	return LayerState{
		Enabled:       s.Enabled,
		Mode:          s.Mode,
		Objectives:    s.Objectives,
		Model:         s.Capability.Model,
		PromptVariant: s.Capability.PromptVariant,
		// inference.used defaults to rules (#741): a cycle that never resolves a
		// backend (rules mode, or a gate-denied llm cycle) honestly reports rules. The
		// loop overwrites this only on a gate-admitted llm cycle with the resolved tier.
		InferenceUsed:             InferenceRules,
		InterventionsAllowed:      s.InterventionsAllowed,
		PolicyBatteryThreshold:    s.Policy.BatteryThreshold,
		PolicyMovementVarianceWin: s.Policy.MovementVarianceWindow,
		PolicyMaxPerMinute:        s.Policy.MaxInterventionsPerMinute,
	}
}

// recordDispatched appends a dispatched outcome.
func (ls *LayerState) recordDispatched(d Decision, weight float64) {
	ls.Candidates++
	ls.Dispatched++
	ls.Outcomes = append(ls.Outcomes, DecisionOutcome{
		Intervention: d.Intervention,
		TargetSerial: d.TargetSerial,
		Dispatched:   true,
		Weight:       weight,
	})
}

// recordBlocked appends a blocked outcome with its attribution.
func (ls *LayerState) recordBlocked(d Decision, reason BlockReason, weight float64) {
	ls.Candidates++
	ls.Blocked++
	ls.Outcomes = append(ls.Outcomes, DecisionOutcome{
		Intervention: d.Intervention,
		TargetSerial: d.TargetSerial,
		Dispatched:   false,
		BlockReason:  reason,
		Weight:       weight,
	})
}
