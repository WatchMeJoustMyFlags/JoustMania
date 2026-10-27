package llm

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
)

// UC: UC-042 BR-007
// TestRetroUser_ShieldHold: the retro prompt of a hold game (#2096) shows the latched
// values and each player's totals; a game without them renders as before.
func TestRetroUser_ShieldHold(t *testing.T) {
	summary := gamesummary.Summary{
		ShieldHold: &gamesummary.ShieldHoldSummary{Activation: "hold", Settings: map[string]float64{"shield.energy_max_seconds": 5, "shield.recharge_per_second": 0.25}},
		PlayerArcs: []gamesummary.PlayerArc{{Serial: "A", ShieldTotals: map[string]float64{"used_seconds": 7.5, "empties": 1}}},
	}
	t.Run("UC-042/BR-007 hold values and totals in the retro", func(t *testing.T) {
		user := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: gamecontext.GameContext{SessionID: "s"}, Summary: summary, Now: fixedNow}).User
		for _, want := range []string{
			"shield_activation: hold (latched for the game)",
			"shield_settings: shield.energy_max_seconds=5 shield.recharge_per_second=0.25",
			"    shield: empties=1 used_seconds=7.5",
		} {
			if !strings.Contains(user, want) {
				t.Errorf("missing %q in:\n%s", want, user)
			}
		}
	})
	t.Run("UC-042/BR-007 no hold values outside a hold game", func(t *testing.T) {
		user := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: gamecontext.GameContext{SessionID: "s"}, Summary: gamesummary.Summary{PlayerArcs: []gamesummary.PlayerArc{{Serial: "A"}}}, Now: fixedNow}).User
		if strings.Contains(user, "shield") {
			t.Errorf("non-hold retro mentions shields:\n%s", user)
		}
	})
}
