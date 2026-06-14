package main

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// experiment_config_flags_test.go is the #1044 loop-level acceptance suite: the
// migrated AGENT_EXPERIMENT_* knobs are now LIVE agent.json flags (read each tick)
// with the env value as the bootstrap default. It proves the startup-gate inversion
// (experiments_enabled off->on->off at runtime), the kill-switch precedence, and the
// ownership invariant (the agent has NO write path to these agent.json flags). The
// per-knob flag-overrides-env / fallback / live-reread behavior is covered in
// flags/flags_test.go (TestExperiment_FlagOverridesEnvDefault), and the registry /
// verdict live-cap reconfigure in experiment/{registry,verdict}_test.go.

// liveConfig is a goroutine-safe settable ExperimentConfig for the configOverride seam.
type liveConfig struct {
	mu  sync.Mutex
	cfg flags.ExperimentConfig
}

func (l *liveConfig) set(c flags.ExperimentConfig) {
	l.mu.Lock()
	l.cfg = c
	l.mu.Unlock()
}

func (l *liveConfig) get(context.Context) flags.ExperimentConfig {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.cfg
}

// loopForConfigTest builds an experimentLoop over a REAL registry + recording
// spawner with a settable live ExperimentConfig (the configOverride seam), so a
// test can flip experiments_enabled / caps at runtime WITHOUT a flagd dependency.
func loopForConfigTest(t *testing.T, maxShadow int) (*experimentLoop, *recordingSpawner, *liveConfig) {
	t.Helper()
	log := testLogger()
	gamePath := newGameFileForLoop(t)
	spawner := &recordingSpawner{}
	writer := experiment.NewWriter(gamePath, log)
	gate := experiment.NewGate(writer, nil, log)
	targeting := experiment.NewGateTargetingWriter(gate, writer)
	reg := experiment.NewRegistry(experiment.RegistryConfig{
		Root:           t.TempDir(),
		MaxShadowGames: maxShadow,
		Spawner:        spawner,
		Verdict:        experiment.NewVerdict(3, 0.5, nil),
		Targeting:      targeting,
		Log:            log,
	})
	lc := &liveConfig{}
	lc.set(flags.ExperimentConfig{Enabled: false}) // default-off, like a fresh agent.json
	loop := &experimentLoop{
		registry: reg,
		log:      log,
		expDefaults: flags.ExperimentDefaults{
			Enabled: false, Tick: time.Hour, DynamicMaxConcurrent: defaultDynamicMaxConcurrent,
		},
		configOverride: lc.get,
		// Kill-switch ON via the seam so experiments_enabled is the variable under test.
		enabledOverride: func(context.Context) bool { return true },
		seed: &experiment.Intent{
			Hypothesis: "h", FlagKey: "invincibility_seconds",
			ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 50,
		},
	}
	return loop, spawner, lc
}

// TestExperimentsEnabled_OffOnOff is the startup-gate-inversion AC (#1044): the loop
// is always running, but a runtime off->on flip STARTS it (rehydrate+seed+allocate)
// and an on->off flip STOPS it cleanly (AbortAll strips targeting + frees capacity),
// all with no restart and no in-flight corruption. The final off->on->off->on cycle
// pins the #1051 one-shot-env-seed semantic: the env seed is declared EXACTLY ONCE
// (on the first enable) and is NOT re-declared on a later re-enable.
func TestExperimentsEnabled_OffOnOff(t *testing.T) {
	loop, spawner, lc := loopForConfigTest(t, 8)
	ctx := context.Background()

	// OFF (default): an allocate tick does nothing — no bootstrap, no live experiment.
	loop.allocateIfEnabled(ctx)
	if loop.startedOnce {
		t.Fatal("disabled loop ran its rehydrate+seed bootstrap; must defer until enabled")
	}
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("disabled loop declared work: LiveCount=%d, want 0", got)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("disabled loop spawned shadow games: %d, want 0", len(recs))
	}

	// Flip ON: the off->on flip must START the loop — bootstrap the seed + allocate.
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
	loop.allocateIfEnabled(ctx)
	if !loop.startedOnce {
		t.Fatal("off->on flip did not run the deferred rehydrate+seed bootstrap")
	}
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("off->on flip did not declare+start the seed experiment: LiveCount=%d, want 1", got)
	}
	if recs := spawner.drain(); len(recs) == 0 {
		t.Fatal("off->on flip did not spawn any shadow game")
	}

	// Flip OFF: the on->off flip must STOP the loop cleanly — AbortAll the live
	// experiment (targeting stripped, capacity freed).
	lc.set(flags.ExperimentConfig{Enabled: false})
	loop.allocateIfEnabled(ctx)
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("on->off flip did not abort the live experiment: LiveCount=%d, want 0", got)
	}
	if n := loop.registry.TotalInFlight(); n != 0 {
		t.Fatalf("on->off flip left in-flight shadow slots: %d, want 0 (no leaked capacity)", n)
	}
	spawner.drain() // discard spawns from the first enable; we measure only the re-enable below.

	// Flip ON again — the off->on->off->on RE-ENABLE (#1051). startedOnce latched on
	// the FIRST enable, so the one-shot env seed is NOT re-declared here: the seed
	// experiment is now terminal (aborted by the on->off teardown) and ensureStarted
	// short-circuits. A re-enable resumes via rehydrate (no non-terminal experiment
	// survives the abort) + the dynamic declarer (unwired in this test) — never by
	// re-running the env seed. So no fresh seed experiment appears and nothing spawns.
	if !loop.startedOnce {
		t.Fatal("startedOnce must stay latched across off->on->off->on (one-shot bootstrap)")
	}
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
	loop.allocateIfEnabled(ctx)
	if !loop.startedOnce {
		t.Fatal("re-enable cleared startedOnce; the env-seed bootstrap must remain one-shot")
	}
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("re-enable RE-DECLARED the one-shot env seed: LiveCount=%d, want 0 (seed is a startup-only trigger; #1051)", got)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("re-enable spawned shadow games off the one-shot seed: %d, want 0 (#1051)", len(recs))
	}
}

// TestExperimentsEnabled_KillSwitchStillWins asserts the master kill-switch
// (agent.json enabled) still beats experiments_enabled: a disabled agent does
// nothing even with experiments_enabled=true.
func TestExperimentsEnabled_KillSwitchStillWins(t *testing.T) {
	loop, spawner, lc := loopForConfigTest(t, 8)
	loop.enabledOverride = func(context.Context) bool { return false } // kill-switch OFF
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})

	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("kill-switch off must override experiments_enabled=true: LiveCount=%d, want 0", got)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("kill-switch off spawned shadow games: %d, want 0", len(recs))
	}
}

// TestExperimentsEnabled_DefaultSafeFreshConfig is the default-safe regression
// (#1044): with NO live config override and a nil flags client (the "fresh
// agent.json + no flag" shape), config() returns the env bootstrap defaults
// verbatim — and the default bootstrap (env unset) keeps experiments OFF, so a fresh
// deployment's behavior is byte-identical to the pre-#1044 disabled default.
func TestExperimentsEnabled_DefaultSafeFreshConfig(t *testing.T) {
	loop, spawner, _ := loopForConfigTest(t, 8)
	loop.configOverride = nil // force the real config() path: nil flags -> expDefaults
	loop.flags = nil
	// expDefaults from loopForConfigTest is Enabled:false (env unset = OFF).
	cfg := loop.config(context.Background())
	if cfg.Enabled {
		t.Fatal("DEFAULT-SAFE VIOLATION: a fresh config enabled experiments")
	}
	loop.allocateIfEnabled(context.Background())
	if loop.registry.LiveCount() != 0 || len(spawner.drain()) != 0 {
		t.Fatal("default-safe loop did work while disabled by default")
	}
}

// TestOwnership_AgentHasNoWritePathToAgentJSON is the ownership invariant (#1044):
// agent.json is human-owned; the agent must NEVER write its own experiment-enable
// flags. The agent's ONLY flagd write path is experiment.Writer/Gate, which are
// hardcoded to game.json (basename guard) and reject any agent.json target — so
// there is no path by which the agent could set experiments_enabled on itself.
func TestOwnership_AgentHasNoWritePathToAgentJSON(t *testing.T) {
	dir := t.TempDir()
	// Stage an agent.json so a (rejected) write would be observable as a mutation.
	agentPath := filepath.Join(dir, "agent.json")
	const agentBefore = `{"metadata":{"flagSetId":"agent"},"flags":{"experiments_enabled":{"state":"ENABLED","variants":{"on":true,"off":false},"defaultVariant":"off"}}}`
	if err := os.WriteFile(agentPath, []byte(agentBefore), 0o644); err != nil {
		t.Fatal(err)
	}

	// The Writer the agent uses is pinned to game.json. Point a Gate at agent.json
	// (the misconfiguration the invariant must catch) and confirm it refuses.
	log := testLogger()
	writer := experiment.NewWriter(agentPath, log) // deliberately wrong target
	gate := experiment.NewGate(writer, nil, log)
	err := gate.Review(context.Background(), experiment.Proposal{
		FlagKey:           "experiments_enabled",
		ExperimentID:      experiment.MintExperimentID(),
		ExperimentalValue: true,
		Rationale:         "agent tries to enable itself (must be blocked)",
	})
	if err == nil {
		t.Fatal("OWNERSHIP VIOLATION: the Gate accepted a write targeting agent.json")
	}
	if !strings.Contains(err.Error(), string(experiment.ReasonNonGameTarget)) {
		t.Fatalf("expected a non_game_target rejection, got: %v", err)
	}
	// The file must be byte-unchanged: no agent.json mutation occurred.
	after, _ := os.ReadFile(agentPath)
	if string(after) != agentBefore {
		t.Fatalf("agent.json was mutated despite the gate rejection:\nbefore=%s\nafter =%s", agentBefore, after)
	}
}
