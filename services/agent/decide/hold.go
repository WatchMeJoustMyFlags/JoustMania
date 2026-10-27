package decide

import (
	"math"
	"strconv"

	"github.com/joustmania/agent/gamecontext"
)

// Hold-mode shield (#2096; UC-040 A36, A37; decide-contract.md §1.2, §4.5 step 4).
// In a game whose game_shield_activation_mode is hold, a grant_shield adds energy
// to the player's pool, and the player is protected only while holding the trigger.
// The state carries the pool from the coordinator's gauges; the agent's shield ledger
// and the rules engine refuse a grant the coordinator would refuse.

// Hold-mode refusals of a targeted grant_shield (decide.block_reason values).
const (
	HoldRefusalBudgetExhausted = "shield_budget_exhausted"
	HoldRefusalPoolFull        = "shield_pool_full"
)

// HoldPoolFullMarginSeconds: a grant is refused shield_pool_full when the energy plus
// the grants in flight is at least the full level minus this margin. A constant
// (C-035): the gauges are up to one export interval old.
const HoldPoolFullMarginSeconds = 0.5

// HoldShieldRefusal is the hold-mode check of a targeted grant_shield, in order:
// shield_budget_exhausted when the budget left is below the minimum burst, then
// shield_pool_full when energy + inFlight is at least min(cap, budget left) minus
// HoldPoolFullMarginSeconds. "" when neither applies. For an active shield only the
// pool check is skipped (the energy gauge over-reads a draining pool); the budget
// check stays, since a draining budget only falls, so refusing early is the safe
// direction. It fails open ("") for every value it needs that is missing or not finite.
func HoldShieldRefusal(energy, budgetLeft *float64, active bool, cap, minBurst *float64, inFlight float64) string {
	if isFiniteSet(budgetLeft) && isFiniteSet(minBurst) && *budgetLeft < *minBurst {
		return HoldRefusalBudgetExhausted
	}
	if active || !isFiniteSet(energy) || !isFiniteSet(cap) {
		return ""
	}
	full := *cap
	if isFiniteSet(budgetLeft) && *budgetLeft < full {
		full = *budgetLeft
	}
	if *energy+inFlight >= full-HoldPoolFullMarginSeconds {
		return HoldRefusalPoolFull
	}
	return ""
}

// HoldRefusal is HoldShieldRefusal for a player of this state ("" outside a hold
// game). The state's energy already includes the grants in flight.
func (s State) HoldRefusal(p PlayerState) string {
	if s.ShieldActivation == "" || !p.Alive {
		return ""
	}
	return HoldShieldRefusal(p.ShieldEnergyS, p.ShieldBudgetRemainingS, p.ShieldActive, s.ShieldEnergyMaxS, s.ShieldMinBurstS, 0)
}

func isFiniteSet(v *float64) bool { return v != nil && !math.IsNaN(*v) && !math.IsInf(*v, 0) }

func finitePtr(v *float64) *float64 {
	if !isFiniteSet(v) {
		return nil
	}
	out := *v
	return &out
}

// applyHoldState fills the hold fields of st from c in a hold game; it does nothing
// in any other game, so their state, wire and digest are unchanged.
func applyHoldState(st *State, c gamecontext.GameContext, ann Annotations) {
	if !c.Session.IsShieldHold() {
		return
	}
	st.ShieldActivation = gamecontext.ShieldActivationHold
	st.ShieldEnergyMaxS = finitePtr(c.Session.ShieldSetting(gamecontext.ShieldSettingEnergyMax))
	st.ShieldBudgetS = finitePtr(c.Session.ShieldSetting(gamecontext.ShieldSettingEnergyBudget))
	st.ShieldRechargePerS = finitePtr(c.Session.ShieldSetting(gamecontext.ShieldSettingRecharge))
	st.ShieldMinBurstS = finitePtr(c.Session.ShieldSetting(gamecontext.ShieldSettingMinBurst))
	for i := range st.Players {
		p := &st.Players[i]
		// No charges in a hold game: shield_charged is not emitted (§1.2).
		p.ShieldCharged = false
		src := c.Players[p.Serial]
		if !p.Alive || src == nil {
			continue
		}
		if energy := finitePtr(src.ShieldEnergy); energy != nil {
			*energy += ann.ShieldInFlight[p.Serial]
			if st.ShieldEnergyMaxS != nil && *energy > *st.ShieldEnergyMaxS {
				*energy = *st.ShieldEnergyMaxS
			}
			p.ShieldEnergyS = energy
		}
		p.ShieldBudgetRemainingS = finitePtr(src.ShieldBudgetRemaining)
		p.ShieldActive = src.ShieldActive != nil && *src.ShieldActive
		if p.ShieldActive {
			p.Shielded = true
		}
	}
}

// holdNarrative is a player's hold token, e.g. " shield_energy=2.4/5 budget=9.1"
// (" shield_active" while held). Empty outside a hold game.
func holdNarrative(p PlayerState, st State) string {
	if st.ShieldActivation == "" || !p.Alive {
		return ""
	}
	var s string
	if p.ShieldEnergyS != nil {
		s += " shield_energy=" + strconv.FormatFloat(*p.ShieldEnergyS, 'f', 1, 64)
		if st.ShieldEnergyMaxS != nil {
			s += "/" + strconv.FormatFloat(*st.ShieldEnergyMaxS, 'f', -1, 64)
		}
	}
	if p.ShieldBudgetRemainingS != nil {
		s += " budget=" + strconv.FormatFloat(*p.ShieldBudgetRemainingS, 'f', 1, 64)
	}
	if p.ShieldActive {
		s += " shield_active"
	}
	return s
}

// holdSessionNarrative is the session line's hold token. Empty outside a hold game.
func holdSessionNarrative(st State) string {
	if st.ShieldActivation == "" {
		return ""
	}
	s := " shield=" + st.ShieldActivation
	for _, kv := range []struct {
		key string
		v   *float64
	}{
		{"shield_cap", st.ShieldEnergyMaxS},
		{"shield_budget", st.ShieldBudgetS},
		{"shield_recharge", st.ShieldRechargePerS},
		{"shield_min_burst", st.ShieldMinBurstS},
	} {
		if kv.v != nil {
			s += " " + kv.key + "=" + strconv.FormatFloat(*kv.v, 'f', -1, 64)
		}
	}
	return s
}
