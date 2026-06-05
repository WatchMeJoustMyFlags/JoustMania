package gamerunner

import (
	"testing"
	"time"
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
	if cfg.GameTimeout != DefaultGameTimeout {
		t.Errorf("GameTimeout = %v, want %v", cfg.GameTimeout, DefaultGameTimeout)
	}
}

func TestConfigFromEnv_Overrides(t *testing.T) {
	t.Setenv(envCoordinatorAddr, "gc:1")
	t.Setenv(envMockAddr, "mc:2")
	t.Setenv(envTimeoutSecs, "30")
	cfg := ConfigFromEnv()
	if cfg.CoordinatorAddr != "gc:1" || cfg.MockAddr != "mc:2" {
		t.Errorf("endpoint overrides not applied: %+v", cfg)
	}
	if cfg.GameTimeout != 30*time.Second {
		t.Errorf("GameTimeout = %v, want 30s", cfg.GameTimeout)
	}
}

func TestSpecFromEnv(t *testing.T) {
	t.Setenv(envMode, "Swapper")
	t.Setenv(envPlayers, "6")
	t.Setenv(envSensitivity, "3")
	spec := SpecFromEnv("rid")
	if spec.RunID != "rid" || spec.GameName != "Swapper" || spec.Players != 6 || spec.Sensitivity != 3 {
		t.Errorf("SpecFromEnv = %+v", spec)
	}
	if spec.Tag() != "agent:rid" {
		t.Errorf("Tag() = %q, want agent:rid", spec.Tag())
	}
}

func TestSpecFromEnv_DefaultsAndGuards(t *testing.T) {
	// Players below 2 and sensitivity out of range fall back to defaults.
	t.Setenv(envPlayers, "1")
	t.Setenv(envSensitivity, "9")
	spec := SpecFromEnv("rid")
	if spec.Players != defaultPlayers {
		t.Errorf("Players = %d, want default %d", spec.Players, defaultPlayers)
	}
	if spec.Sensitivity != defaultSensitivity {
		t.Errorf("Sensitivity = %d, want default %d", spec.Sensitivity, defaultSensitivity)
	}
	if spec.GameName != defaultMode {
		t.Errorf("GameName = %q, want default %q", spec.GameName, defaultMode)
	}
}
