package main

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// experiment_flag_uc040_test.go covers the decision loops' late-bound view of the
// flag under test (UC-040 A40, BR-028; #1975): nothing before the registry exists,
// then the registry's answer by game id and experiment id.

// UC: UC-040 A40 BR-028
func TestExperimentFlagLookup(t *testing.T) {
	ctx := gamecontext.GameContext{SessionID: "game_1", ExperimentID: "exp_1", Arm: "control"}
	t.Run("UC-040/A40 before the registry is installed nothing is skipped", func(t *testing.T) {
		var l experimentFlagLookup
		if got := l.FlagFor(ctx); got != "" {
			t.Fatalf("FlagFor before Set = %q, want \"\"", got)
		}
	})
	t.Run("UC-040/A40 BR-028 the installed registry answers by game id and experiment id", func(t *testing.T) {
		var l experimentFlagLookup
		var gotGame, gotExp string
		l.Set(func(gameID, experimentID string) (string, bool) {
			gotGame, gotExp = gameID, experimentID
			return "windows", true
		})
		if got := l.FlagFor(ctx); got != "windows" {
			t.Fatalf("FlagFor = %q, want windows", got)
		}
		if gotGame != "game_1" || gotExp != "exp_1" {
			t.Fatalf("lookup called with (%q, %q), want (game_1, exp_1)", gotGame, gotExp)
		}
	})
	t.Run("UC-040/A40 step 4 an experiment the registry does not know skips nothing", func(t *testing.T) {
		var l experimentFlagLookup
		l.Set(func(string, string) (string, bool) { return "", false })
		if got := l.FlagFor(ctx); got != "" {
			t.Fatalf("FlagFor unknown = %q, want \"\"", got)
		}
	})
}
