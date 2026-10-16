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

// budgetRefuses is step 3 of the permission chain: it reports whether the
// weighted per-game budget refuses d (charging cost when it admits). state
// carries the cycle's hold (BR-014): once a decision that could fit an empty
// window is refused, every lower-ranked decision of the same cycle is refused
// without asking the limiter.
func (l *Loop) budgetRefuses(snapshot flags.Snapshot, d Decision, now time.Time, cost float64, state *LayerState) bool {
	if state != nil && state.budgetHeld {
		return true
	}
	budget := float64(snapshot.Policy.MaxInterventionsPerMinute)
	reserve := nudgeReserve(d, snapshot.Objectives)
	if l.limiter.admit(now, cost, budget, reserve) {
		return false
	}
	// A nudge refused for the reserve does not hold the cycle: a lower-ranked
	// protective intervention may still use the reserve. A decision costing more
	// than the whole budget can never fit and does not hold it either.
	if state != nil && reserve == 0 && cost <= budget {
		state.budgetHeld = true
	}
	return true
}

// nudgeReserve is the budget d must leave free (BR-015): protectiveReserve for a
// send_controller_effect, unless chaos is the strictly dominant objective in the
// decision's weights (else the objectives flag, else the engine defaults); 0 for
// every other intervention.
func nudgeReserve(d Decision, flagObjectives map[string]float64) float64 {
	if d.Intervention != InterventionSendControllerEffect {
		return 0
	}
	weights := d.Objectives
	if len(weights) == 0 {
		weights = flagObjectives
	}
	if objectiveDominant(normalizedWeights(weights), ObjectiveChaos) {
		return 0
	}
	return protectiveReserve
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

// recordRepeatRefusal records a repeated rate_limit refusal without spans,
// warning or timeline entry (A21): the outcome stays in the cycle's LayerState
// (so agent.decide still reports decide.blocked) and in agent_decisions_total.
func (l *Loop) recordRepeatRefusal(ctx context.Context, d Decision, cost float64, state *LayerState) {
	state.recordBlocked(d, ReasonRateLimit, cost)
	l.recordDecision(ctx, d.Intervention, true, ReasonRateLimit)
	l.Log.Debug("agent.decision_rate_limit_repeat",
		"intervention", d.Intervention,
		"target_serial", d.TargetSerial)
}
