package gamesummary

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-042 BR-007
// TestBuildSummary_ShieldHold: the summary of a hold game (#2096) carries the latched
// values and each player's totals from the hold gauges; every other game's summary
// has neither.
func TestBuildSummary_ShieldHold(t *testing.T) {
	record := func(mode string) func(s *gamecontext.Store) {
		return func(s *gamecontext.Store) {
			s.SetShieldActivation(mode)
			s.SetShieldSetting(gamecontext.ShieldSettingEnergyMax, 5)
			s.SetShieldSetting("shield.hold_rumble_intensity", 70)
			s.SetPlayerAlive("A", true)
			s.SetPlayerShieldTotal("A", "granted_seconds", 4)
			s.SetPlayerShieldTotal("A", "activations", 3)
		}
	}
	t.Run("UC-042/BR-007 a hold game carries the latched values and per-player totals", func(t *testing.T) {
		got := BuildSummary(endedGame(t, record("hold")), BuildOptions{Now: time.Now()})
		if got.ShieldHold == nil || got.ShieldHold.Activation != "hold" ||
			got.ShieldHold.Settings[gamecontext.ShieldSettingEnergyMax] != 5 || got.ShieldHold.Settings["shield.hold_rumble_intensity"] != 70 {
			t.Fatalf("shield_hold = %+v", got.ShieldHold)
		}
		if len(got.PlayerArcs) != 1 || got.PlayerArcs[0].ShieldTotals["granted_seconds"] != 4 || got.PlayerArcs[0].ShieldTotals["activations"] != 3 {
			t.Fatalf("arcs = %+v", got.PlayerArcs)
		}
	})
	t.Run("UC-042/BR-007 a trigger game's summary has no hold values", func(t *testing.T) {
		got := BuildSummary(endedGame(t, record("trigger")), BuildOptions{Now: time.Now()})
		raw, _ := json.Marshal(got)
		if got.ShieldHold != nil || strings.Contains(string(raw), "shield_") {
			t.Fatalf("non-hold summary carries hold values: %s", raw)
		}
	})
}
