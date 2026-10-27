package jev

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// withPlayer returns the fixture request's state with MOCK0003 changed by edit.
func withPlayer(t *testing.T, edit func(p *decide.PlayerState)) decide.State {
	t.Helper()
	st := testRequest().State
	players := make([]decide.PlayerState, len(st.Players))
	copy(players, st.Players)
	found := false
	for i := range players {
		if players[i].Serial == "MOCK0003" {
			edit(&players[i])
			found = true
		}
	}
	if !found {
		t.Fatal("fixture lacks MOCK0003")
	}
	st.Players = players
	return st
}

// UC: UC-040 A33 A35
func TestSuitability_ShieldLimitedAndLevels(t *testing.T) {
	t.Run("UC-040/A33 grant_shield to a shield_limited player reads as refused", func(t *testing.T) {
		st := withPlayer(t, func(p *decide.PlayerState) { p.ShieldLimited = true })
		d := describeCandidates(st, "grant_shield")["MOCK0003"]
		if !strings.Contains(d, "No more agent shields for this player right now (per-game cap or cooldown): another shield is refused.") {
			t.Errorf("grant_shield description lacks the limit text: %q", d)
		}
		if strings.Contains(d, "matters most") {
			t.Errorf("grant_shield description still recommends protection: %q", d)
		}
		if d := describeCandidates(st, "toughness_boost")["MOCK0003"]; strings.Contains(d, "No more agent shields") {
			t.Errorf("toughness_boost is not limited: %q", d)
		}
	})
	t.Run("UC-040/A35 the candidate description names an applied factor", func(t *testing.T) {
		f := 0.8
		st := withPlayer(t, func(p *decide.PlayerState) { p.SensitivityFactor = &f })
		if d := describeCandidates(st, "adjust_player_sensitivity")["MOCK0003"]; !strings.Contains(d, "Agent sensitivity factor 0.80 already applied.") {
			t.Errorf("description lacks the applied factor: %q", d)
		}
		if d := describeCandidates(testRequest().State, "adjust_player_sensitivity")["MOCK0003"]; strings.Contains(d, "already applied") {
			t.Errorf("no factor applied, but described: %q", d)
		}
	})
}
