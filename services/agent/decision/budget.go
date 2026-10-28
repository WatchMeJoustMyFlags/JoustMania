package decision

import (
	"context"
	"time"

	"github.com/joustmania/agent/flags"
)

// budget.go is the budget-aware admission step of the permission chain
// (UC-040 A20/A21, BR-014/BR-015, #1326). The weighted per-game limiter
// (ratelimit.go) only answers "does this cost fit"; this file decides which of a
// cycle's decisions may ask, so a scarce budget is spent on the most valuable
// one:
//
//   - BR-014: decisions reach the limiter in rank order, and the first one that
//     does not fit ends admission for the cycle. Without it a 0.5 nudge ranked
//     below a 1.0 shield took every half-free budget the shield could not use,
//     and the shield never applied (dry run c8e9aa27: 0 of 49 shields).
//   - BR-015: a send_controller_effect must leave protectiveReserve free, unless
//     chaos strictly dominates the objective weights.
//   - A21: a refusal that repeats while the budget state is unchanged is counted
//     and logged at debug level, but emits no agent.decision span.
//
// When the budget covers every decision of a cycle none of this changes the
// outcome, so the rules path golden is unchanged.

// protectiveReserve is the budget a nudge must leave free: one medium
// intervention, the cost of grant_shield and adjust_player_sensitivity.
var protectiveReserve = interventionCost(InterventionGrantShield)

// admitOrHold is step 5 of the permission chain. It CHARGES the budget when it
// admits d (returning true) and, when it refuses, may SET the cycle's hold on
// state (BR-014): once a decision that could fit an empty window is refused,
// every lower-ranked decision of the same cycle is refused without asking the
// limiter. It is not a pure check.
func (l *Loop) admitOrHold(snapshot flags.Snapshot, d Decision, now time.Time, cost float64, state *LayerState) bool {
	if state != nil && state.budgetHeld {
		return false
	}
	budget := float64(snapshot.Policy.MaxInterventionsPerMinute)
	reserve := nudgeReserve(d, snapshot)
	if l.limiter.admit(now, cost, budget, reserve) {
		return true
	}
	// A nudge refused for the reserve does not hold the cycle: a lower-ranked
	// protective intervention may still use the reserve. A decision costing more
	// than the whole budget can never fit and does not hold it either.
	if state != nil && reserve == 0 && cost <= budget {
		state.budgetHeld = true
	}
	return false
}

// protectiveInterventions are the medium-cost interventions the nudge reserve
// keeps room for (BR-015).
var protectiveInterventions = []string{
	InterventionGrantShield,
	InterventionToughnessBoost,
	InterventionAdjustPlayerSensitivity,
	InterventionSetPlayerHandicap,
}

// nudgeReserve is the budget d must leave free (BR-015): protectiveReserve for a
// send_controller_effect, unless chaos is the strictly dominant objective in the
// decision's weights (else the objectives flag, else the engine defaults) or the
// allow-list permits no protective intervention (then nothing could use the
// reserve, e.g. the shipped `ambient`); 0 for every other intervention.
func nudgeReserve(d Decision, snapshot flags.Snapshot) float64 {
	if d.Intervention != InterventionSendControllerEffect {
		return 0
	}
	weights := d.Objectives
	if len(weights) == 0 {
		weights = snapshot.Objectives
	}
	if objectiveDominant(normalizedWeights(weights), ObjectiveChaos) {
		return 0
	}
	for _, p := range protectiveInterventions {
		if snapshot.Permits(p) {
			return protectiveReserve
		}
	}
	return 0
}

// objectiveDominant reports whether name is the strict argmax of weights. A tie
// is not dominance.
func objectiveDominant(weights map[string]float64, name string) bool {
	w, ok := weights[name]
	if !ok {
		return false
	}
	for other, ow := range weights {
		if other != name && ow >= w {
			return false
		}
	}
	return true
}

// refusalKey identifies a decision for A21's repeat check.
func refusalKey(d Decision) string { return d.Intervention + "|" + d.TargetSerial }

// repeatRefusal reports whether a rate_limit refusal of d repeats one already
// reported in the current budget state (A21).
func (l *Loop) repeatRefusal(d Decision, now time.Time) bool {
	return !l.limiter.firstRefusal(refusalKey(d), now)
}

// recordRepeatRefusal records a repeated rate_limit (A21) or already_charged (A31)
// refusal without spans, warning or timeline entry: the outcome stays in the cycle's
// LayerState (so agent.decide still reports decide.blocked) and in
// agent_decisions_total, and a debug line names the repeat.
func (l *Loop) recordRepeatRefusal(ctx context.Context, gameKind string, d Decision, cost float64, state *LayerState, reason BlockReason) {
	state.recordBlocked(d, reason, cost)
	l.recordDecision(ctx, gameKind, d.Intervention, true, reason)
	l.Log.Debug("agent.decision_"+string(reason)+"_repeat",
		"intervention", d.Intervention,
		"target_serial", d.TargetSerial)
}
