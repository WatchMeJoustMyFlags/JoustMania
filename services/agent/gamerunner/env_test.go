package gamerunner

import (
	"testing"
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
