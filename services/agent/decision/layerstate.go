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
		Enabled:                   s.Enabled,
		Mode:                      s.Mode,
		Objectives:                s.Objectives,
		Model:                     s.Capability.Model,
		PromptVariant:             s.Capability.PromptVariant,
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
