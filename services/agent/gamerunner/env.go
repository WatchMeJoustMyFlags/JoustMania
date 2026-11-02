package gamerunner

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Trigger gating + spec are resolved from environment variables. This is the
// single, minimal trigger path for phase 4 (see PR notes for the flagd-nonce
// alternative): when AGENT_SHADOW_GAME=true the agent runs ONE shadow game at
// startup using the SHADOW_GAME_* spec, then continues its normal loop. It
// keeps the PR self-contained in services/agent (no flagd-schema change). It
// mirrors the original env-gate shape the interventions/rollout actuation gates
// used before #1213 migrated those two to LIVE agent.json flags
// (interventions_enabled / rollout_enabled); AGENT_SHADOW_GAME remains a one-shot
// startup trigger, so it stays an env var.
const (
	envEnabled     = "AGENT_SHADOW_GAME"
	envMode        = "SHADOW_GAME_MODE"
	envSensitivity = "SHADOW_GAME_SENSITIVITY"

	// Experiment binding of the one-shot game (#2530), both or neither: an operator
	// A/B script (scripts/agent-group-ab.sh) binds each game to its arm so arm values
	// of that experiment resolve in it.
	envExperimentID = "SHADOW_GAME_EXPERIMENT_ID"
	envArm          = "SHADOW_GAME_ARM"

	// Endpoint overrides (default to the docker-compose service addresses).
	envCoordinatorAddr = "GAME_COORDINATOR_ADDR"
	envMockAddr        = "MOCK_CONTROLLER_ADDR"
)

// Defaults for the env-triggered spec.
const (
	defaultMode        = "JoustFFA"
	defaultSensitivity = 2
)

// Enabled reports whether the env trigger requests a shadow-game run.
func Enabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(envEnabled)), "true")
}

// ConfigFromEnv builds a Config from the endpoint env overrides, falling back to
// the docker-compose service defaults.
func ConfigFromEnv() Config {
	cfg := Config{
		CoordinatorAddr: getEnv(envCoordinatorAddr, DefaultCoordinatorAddr),
		MockAddr:        getEnv(envMockAddr, DefaultMockAddr),
	}
	return cfg.withDefaults()
}

// SpecFromEnv builds the shadow-game Spec from SHADOW_GAME_* env vars, applying
// defaults. runID is supplied by the caller (a per-run unique id).
func SpecFromEnv(runID string) Spec {
	sensitivity := defaultSensitivity
	if n, ok := envInt(envSensitivity); ok && n >= 0 && n <= 4 {
		sensitivity = n
	}
	return Spec{
		RunID:       runID,
		GameName:    getEnv(envMode, defaultMode),
		Sensitivity: sensitivity,
	}
}

// ExperimentFromEnv reads the one-shot game's experiment binding (#2530):
// SHADOW_GAME_EXPERIMENT_ID and SHADOW_GAME_ARM, both or neither. Neither gives
// ("", "", nil), an unbound game as before; exactly one is an error, and the
// caller refuses the run rather than start a game that silently ignores the arm.
func ExperimentFromEnv() (experimentID, arm string, err error) {
	experimentID = strings.TrimSpace(os.Getenv(envExperimentID))
	arm = strings.TrimSpace(os.Getenv(envArm))
	if (experimentID == "") != (arm == "") {
		return "", "", errors.New(envExperimentID + " and " + envArm + " must be set together")
	}
	return experimentID, arm, nil
}

func getEnv(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// envInt parses an integer env var. ok is false when unset or unparseable.
func envInt(key string) (int, bool) {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return 0, false
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return 0, false
	}
	return n, true
}

// String renders a Spec for logging.
func (s Spec) String() string {
	return fmt.Sprintf("run_id=%s mode=%s players=%d sensitivity=%d",
		s.RunID, s.GameName, s.Players, s.Sensitivity)
}
