package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment"
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

// The production wiring: a lookup installed from a real registry answers the flag
// for a game id the registry bound at spawn.
// UC: UC-040 A40 BR-028
func TestInstallExperimentFlags_RealRegistry(t *testing.T) {
	spawner := &recordingSpawner{}
	reg := experiment.NewRegistry(experiment.RegistryConfig{Root: t.TempDir(), MaxShadowGames: 2, Spawner: spawner, Log: testLogger()})
	id, err := reg.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "windows", ExperimentalValue: 0.5, Objective: "balanced", TargetNPerArm: 2,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	ctx := context.Background()
	if err := reg.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := reg.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("no game spawned")
	}
	calls := spawner.drain()
	var l experimentFlagLookup
	installExperimentFlags(&l, reg)
	for _, c := range calls {
		if got := l.FlagFor(gamecontext.GameContext{SessionID: c.gameID}); got != "windows" {
			t.Fatalf("FlagFor(bound game %s) = %q, want windows", c.gameID, got)
		}
	}
}
