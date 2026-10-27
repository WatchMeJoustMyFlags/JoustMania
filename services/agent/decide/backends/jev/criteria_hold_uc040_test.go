package jev

import (
	"encoding/json"
	"math"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// holdState is the fixture request's state as a hold game (#2096): MOCK0003 with a
// full pool, every other alive player with room.
func holdState(t *testing.T, budgetLeft float64) decide.State {
	t.Helper()
	st := testRequest().State
	st.ShieldActivation = "hold"
	st.ShieldEnergyMaxS, st.ShieldBudgetS, st.ShieldRechargePerS, st.ShieldMinBurstS = f64(5), f64(15), f64(0), f64(0.3)
	players := make([]decide.PlayerState, len(st.Players))
	copy(players, st.Players)
	found := false
	for i := range players {
		players[i].ShieldEnergyS, players[i].ShieldBudgetRemainingS = f64(1), f64(12)
		if players[i].Serial == "MOCK0003" {
			players[i].ShieldEnergyS, players[i].ShieldBudgetRemainingS = f64(4.8), f64(budgetLeft)
			found = true
		}
	}
	if !found {
		t.Fatal("fixture lacks MOCK0003")
	}
	st.Players = players
	return st
}

// UC: UC-040 A37
func TestSuitability_Hold(t *testing.T) {
	t.Run("UC-040/A37 grant_shield into a full pool reads as refused", func(t *testing.T) {
		d := describeCandidates(holdState(t, 12), "grant_shield")["MOCK0003"]
		if !strings.Contains(d, "Shield energy already full: another shield is refused.") {
			t.Errorf("description lacks the pool refusal: %q", d)
		}
		if !strings.Contains(d, "Shield energy 4.8 of 5 s, 12.0 s of shield budget left.") {
			t.Errorf("description lacks the pool: %q", d)
		}
	})
	t.Run("UC-040/A37 grant_shield with the budget used up reads as refused", func(t *testing.T) {
		d := describeCandidates(holdState(t, 0.1), "grant_shield")["MOCK0003"]
		if !strings.Contains(d, "Shield budget for this game used up") {
			t.Errorf("description lacks the budget refusal: %q", d)
		}
	})
	t.Run("UC-040/A36 a toughness_boost keeps the danger text", func(t *testing.T) {
		d := describeCandidates(holdState(t, 12), "toughness_boost")["MOCK0003"]
		if strings.Contains(d, "is refused") {
			t.Errorf("toughness_boost must not read as refused: %q", d)
		}
	})
	t.Run("UC-040/A37 outside a hold game nothing changes", func(t *testing.T) {
		st := testRequest().State
		for _, p := range st.Players {
			if strings.Contains(describeCandidates(st, "grant_shield")[p.Serial], "Shield energy") {
				t.Fatalf("non-hold description mentions shield energy: %s", p.Serial)
			}
		}
	})
}

// UC: UC-040 A36
func TestEncodeState_Hold(t *testing.T) {
	t.Run("UC-040/A36 the wire carries the hold values in a hold game only", func(t *testing.T) {
		st := holdState(t, 12)
		st.Players[0].ShieldEnergyS = f64(math.NaN())
		raw, err := json.Marshal(encodeState(st, ""))
		if err != nil {
			t.Fatalf("marshal: %v (a NaN must be dropped)", err)
		}
		for _, want := range []string{`"shield_activation":"hold"`, `"shield_energy_max_s":5`, `"shield_min_burst_s":0.3`, `"shield_energy_s":4.8`} {
			if !strings.Contains(string(raw), want) {
				t.Errorf("wire lacks %s: %s", want, raw)
			}
		}
		plain, _ := json.Marshal(encodeState(testRequest().State, ""))
		if strings.Contains(string(plain), "shield_") {
			t.Errorf("non-hold wire carries shield fields: %s", plain)
		}
	})
}
