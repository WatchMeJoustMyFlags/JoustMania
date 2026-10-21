package jev

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// shieldChargedState is the fixture request's state with the near-threshold
// player MOCK0003 marked shield_charged (#1870).
func shieldChargedState(t *testing.T) decide.State {
	t.Helper()
	st := testRequest().State
	players := make([]decide.PlayerState, len(st.Players))
	copy(players, st.Players)
	found := false
	for i := range players {
		if players[i].Serial == "MOCK0003" {
			players[i].ShieldCharged = true
			found = true
		}
	}
	if !found {
		t.Fatal("fixture lacks MOCK0003")
	}
	st.Players = players
	return st
}

// TestSuitability_ShieldCharged: a shield_charged player is described as such, a
// grant_shield to them is refused, and a toughness_boost keeps the danger text.
//
// UC: UC-040 A31
func TestSuitability_ShieldCharged(t *testing.T) {
	st := shieldChargedState(t)
	t.Run("UC-040/A31 grant_shield to a charged player reads as refused", func(t *testing.T) {
		d := describeCandidates(st, "grant_shield")["MOCK0003"]
		if !strings.Contains(d, "another shield is refused") {
			t.Errorf("grant_shield description lacks the refusal: %q", d)
		}
		if strings.Contains(d, "matters most") {
			t.Errorf("grant_shield description still recommends protection: %q", d)
		}
	})
	t.Run("UC-040/A31 toughness_boost to a charged player keeps the danger text", func(t *testing.T) {
		d := describeCandidates(st, "toughness_boost")["MOCK0003"]
		if !strings.Contains(d, "protecting this player now matters most") || strings.Contains(d, "another shield is refused") {
			t.Errorf("toughness_boost description = %q, want the danger text", d)
		}
	})
	t.Run("UC-040/A31 the candidate description names the charge", func(t *testing.T) {
		for _, iv := range []string{"grant_shield", "soft_penalty"} {
			if d := describeCandidates(st, iv)["MOCK0003"]; !strings.Contains(d, "Holds an agent shield charge, or has just used it.") {
				t.Errorf("%s description lacks the charge sentence: %q", iv, d)
			}
		}
		if d := describeCandidates(testRequest().State, "grant_shield")["MOCK0003"]; strings.Contains(d, "shield charge") {
			t.Errorf("an uncharged player must not mention a charge: %q", d)
		}
	})
}
