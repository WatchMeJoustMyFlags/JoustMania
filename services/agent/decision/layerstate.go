package decision

import (
	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

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
	// ReasonAlreadyCharged: in trigger mode the agent's charge ledger holds a
	// grant_shield to this player within its window (UC-040 A31 step 1, BR-020).
	ReasonAlreadyCharged BlockReason = "already_charged"
	// ReasonShieldCapReached: the agent already wrote
	// policy.max_shields_per_player_per_game shields to this player in this game
	// (written plus in flight; UC-040 A33, BR-022).
	ReasonShieldCapReached BlockReason = "shield_cap_reached"
	// ReasonShieldCooldown: the player's last agent shield ended less than
	// policy.shield_cooldown_seconds ago, or another grant to them is in flight
	// (UC-040 A33, BR-022).
	ReasonShieldCooldown BlockReason = "shield_cooldown"
	// ReasonShieldBudgetExhausted / ReasonShieldPoolFull: in a hold game the target's
	// shield budget left is below the minimum burst, or its energy plus the grants in
	// flight is within 0.5 s of full (UC-040 A37 step 1, BR-024; #2096).
	ReasonShieldBudgetExhausted BlockReason = decide.HoldRefusalBudgetExhausted
	ReasonShieldPoolFull        BlockReason = decide.HoldRefusalPoolFull
	// ReasonAlreadyApplied: a state or level decision repeats the value the agent
	// last wrote for the same intervention and target in this game; nothing is
	// written (UC-040 A35, BR-023). Not a refusal by a gate: the value is in place.
	ReasonAlreadyApplied BlockReason = "already_applied"
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
//	Existence:   Enabled
//	Objective:   Objectives
//	Backend:     DecideBackend
//	Permission:  InterventionsAllowed + the three Policy* values
//	Outcomes:    per-decision dispatch/block results
type LayerState struct {
	// --- Existence layer ---
	Enabled bool

	// --- Objective layer ---
	Objectives map[string]float64

	// --- Permission layer ---
	InterventionsAllowed   []string
	PolicyBatteryThreshold int
	PolicyMaxPerMinute     int

	// --- Fitness layer (#731 hook) ---
	// FitnessEvaluated holds the cycle-level fitness-function results that #731
	// will populate (e.g. session_duration, target_session_seconds). It is the
	// span-attribute hook for fitness.evaluated at the cycle scope: empty/absent
	// until #731 wires the fitness engine, lifted onto the decision span only
	// when non-empty. Per-decision fitness already rides on Decision.Fitness and
	// the per-decision fitness.evaluated attribute; this is the cycle-wide view.
	FitnessEvaluated map[string]float64

	// DecideBackend is the decision backend that produced this cycle's decisions
	// (#1226): "rules", or the remote backend's name. Empty when the decide hook
	// did not run (engines without the decide seam), so their spans are unchanged.
	DecideBackend string

	// --- Per-decision outcomes ---
	Candidates int
	Dispatched int
	Blocked    int
	Outcomes   []DecisionOutcome

	// budgetHeld is set once a decision of this cycle that could fit an empty
	// window was refused by the rate limit: every lower-ranked decision of the
	// cycle is then refused without charge (UC-040 BR-014, budget.go).
	budgetHeld bool
}

// newLayerState seeds a LayerState from the evaluated flag snapshot. Outcomes
// are filled in as decisions are processed.
func newLayerState(s flags.Snapshot) LayerState {
	return LayerState{
		Enabled:                s.Enabled,
		Objectives:             s.Objectives,
		InterventionsAllowed:   s.InterventionsAllowed,
		PolicyBatteryThreshold: s.Policy.BatteryThreshold,
		PolicyMaxPerMinute:     s.Policy.MaxInterventionsPerMinute,
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
