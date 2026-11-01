package llmio

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 A36 A37
// TestRender_HoldGame: an LLM prompt of a hold game (#2096) carries the pool in the
// state, and the static system prompt explains the hold tokens.
func TestRender_HoldGame(t *testing.T) {
	t.Run("UC-040/A36 the hold tokens reach the prompt", func(t *testing.T) {
		mode := gamecontext.ShieldActivationHold
		active := true
		energy, budget := 2.4, 9.1
		c := gamecontext.GameContext{SessionID: "g1", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{
			// No movement readings: A is marked readings_stale (#2514); the hold tokens stay.
			"A": {Serial: "A", Active: &active, ShieldEnergy: &energy, ShieldBudgetRemaining: &budget},
		}}
		c.Session.ShieldActivation = &mode
		c.Session.ShieldSettings = map[string]float64{gamecontext.ShieldSettingEnergyMax: 5}
		st := decide.BuildState(c)
		p := Render(decide.Request{State: st, Questions: decide.FFAQuestions(st, []string{"grant_shield"})})
		for _, want := range []string{" shield=hold shield_cap=5", "A alive readings_stale shield_energy=2.4/5 budget=9.1"} {
			if !strings.Contains(p.User, want) {
				t.Errorf("user prompt lacks %q:\n%s", want, p.User)
			}
		}
		if !strings.Contains(p.System, `In a hold game (the session line says "shield=hold")`) {
			t.Error("system prompt does not explain the hold tokens")
		}
	})
}
