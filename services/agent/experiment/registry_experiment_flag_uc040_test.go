package experiment

import (
	"context"
	"testing"
)

// registry_experiment_flag_uc040_test.go covers the registry's lookup of the flag
// under test in a game (UC-040 A40, BR-028; #1975): the game id bound at spawn
// answers for both arms, then the experiment id; an unknown game and experiment
// answer nothing.

// UC: UC-040 A40 BR-028
func TestRegistry_ExperimentFlag(t *testing.T) {
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 2,
		Spawner:        spawner,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	in := sampleIntent()
	in.FlagKey = "windows"
	id, err := r.Declare(in)
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := r.AllocateAndSpawn(ctx); n != 2 {
		t.Fatalf("spawned %d, want 2", n)
	}

	t.Run("UC-040/A40 BR-028 a game bound at spawn answers its experiment's flag in either arm", func(t *testing.T) {
		arms := map[string]bool{}
		for _, call := range spawner.calls {
			arms[call.arm] = true
			if flag, ok := r.ExperimentFlag(call.gameID, ""); !ok || flag != "windows" {
				t.Fatalf("ExperimentFlag(%s, \"\") = (%q, %v), want (windows, true)", call.gameID, flag, ok)
			}
		}
		if !arms[ArmExperimental] || !arms[ArmControl] {
			t.Fatalf("spawned arms = %v, want both", arms)
		}
	})
	t.Run("UC-040/A40 BR-028 an unbound game answers by its experiment id", func(t *testing.T) {
		if flag, ok := r.ExperimentFlag("game_unbound", id); !ok || flag != "windows" {
			t.Fatalf("ExperimentFlag(unbound, id) = (%q, %v), want (windows, true)", flag, ok)
		}
	})
	t.Run("UC-040/A40 an unknown game and experiment answer nothing", func(t *testing.T) {
		if flag, ok := r.ExperimentFlag("game_unbound", "exp_unknown"); ok || flag != "" {
			t.Fatalf("ExperimentFlag(unknown) = (%q, %v), want (\"\", false)", flag, ok)
		}
		if flag, ok := r.ExperimentFlag("", ""); ok || flag != "" {
			t.Fatalf("ExperimentFlag(\"\", \"\") = (%q, %v), want (\"\", false)", flag, ok)
		}
	})
	t.Run("UC-040/A40 step 4 after Abort the experiment id answers nothing", func(t *testing.T) {
		if err := r.Abort(ctx, id, "test"); err != nil {
			t.Fatalf("Abort: %v", err)
		}
		if flag, ok := r.ExperimentFlag("game_unbound", id); ok || flag != "" {
			t.Fatalf("ExperimentFlag after Abort = (%q, %v), want (\"\", false)", flag, ok)
		}
	})
}
