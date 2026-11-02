package gamerunner

import (
	"testing"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

func TestEnabled(t *testing.T) {
	t.Setenv(envEnabled, "true")
	if !Enabled() {
		t.Error("Enabled() = false, want true for AGENT_SHADOW_GAME=true")
	}
	t.Setenv(envEnabled, "FALSE")
	if Enabled() {
		t.Error("Enabled() = true, want false for AGENT_SHADOW_GAME=FALSE")
	}
}

func TestConfigFromEnv_Defaults(t *testing.T) {
	cfg := ConfigFromEnv()
	if cfg.CoordinatorAddr != DefaultCoordinatorAddr {
		t.Errorf("CoordinatorAddr = %q, want %q", cfg.CoordinatorAddr, DefaultCoordinatorAddr)
	}
	if cfg.MockAddr != DefaultMockAddr {
		t.Errorf("MockAddr = %q, want %q", cfg.MockAddr, DefaultMockAddr)
	}
}

func TestConfigFromEnv_Overrides(t *testing.T) {
	t.Setenv(envCoordinatorAddr, "gc:1")
	t.Setenv(envMockAddr, "mc:2")
	cfg := ConfigFromEnv()
	if cfg.CoordinatorAddr != "gc:1" || cfg.MockAddr != "mc:2" {
		t.Errorf("endpoint overrides not applied: %+v", cfg)
	}
}

func TestSpecFromEnv(t *testing.T) {
	t.Setenv(envMode, "Swapper")
	t.Setenv(envSensitivity, "3")
	spec := SpecFromEnv("rid")
	if spec.RunID != "rid" || spec.GameName != "Swapper" || spec.Sensitivity != 3 {
		t.Errorf("SpecFromEnv = %+v", spec)
	}
	if spec.Tag() != "agent:rid" {
		t.Errorf("Tag() = %q, want agent:rid", spec.Tag())
	}
}

func TestSpecFromEnv_DefaultsAndGuards(t *testing.T) {
	// Sensitivity out of range falls back to the default; players come from the archetype.
	t.Setenv(envSensitivity, "9")
	spec := SpecFromEnv("rid")
	if spec.Sensitivity != defaultSensitivity {
		t.Errorf("Sensitivity = %d, want default %d", spec.Sensitivity, defaultSensitivity)
	}
	if spec.GameName != defaultMode {
		t.Errorf("GameName = %q, want default %q", spec.GameName, defaultMode)
	}
}

// UC: UC-040 BR-036
// The one-shot runner's experiment binding (#2530): both env vars bind the game, neither
// leaves it unbound, exactly one refuses the run.
func TestExperimentFromEnv(t *testing.T) {
	t.Run("UC-040/BR-036 both bind the game to the arm", func(t *testing.T) {
		t.Setenv(envExperimentID, " group-ab-1-2 ")
		t.Setenv(envArm, "B")
		id, arm, err := ExperimentFromEnv()
		if err != nil || id != "group-ab-1-2" || arm != "B" {
			t.Fatalf("got %q %q %v", id, arm, err)
		}
	})
	t.Run("UC-040/BR-036 neither leaves the game unbound", func(t *testing.T) {
		t.Setenv(envExperimentID, "")
		t.Setenv(envArm, "")
		id, arm, err := ExperimentFromEnv()
		if err != nil || id != "" || arm != "" {
			t.Fatalf("got %q %q %v", id, arm, err)
		}
	})
	for _, tc := range []struct{ name, id, arm string }{
		{"UC-040/BR-036 an experiment id without an arm refuses", "group-ab-1-2", ""},
		{"UC-040/BR-036 an arm without an experiment id refuses", "", "A"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envExperimentID, tc.id)
			t.Setenv(envArm, tc.arm)
			if _, _, err := ExperimentFromEnv(); err == nil {
				t.Fatal("want an error")
			}
		})
	}
	t.Run("UC-040/BR-036 a bound spec starts the game with origin AGENT and the ids", func(t *testing.T) {
		spec := Spec{GameName: "JoustFFA", Sensitivity: 2, ExperimentID: "group-ab-1-2", Arm: "A"}
		cfg := buildStartConfig(spec, []string{"a", "b"})
		if cfg.Origin != gcpb.GameOrigin_GAME_ORIGIN_AGENT || cfg.ExperimentId != "group-ab-1-2" || cfg.Arm != "A" {
			t.Fatalf("start config = %v", cfg)
		}
	})
}
