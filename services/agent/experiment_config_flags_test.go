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
// with the compiled default (equal to the flag default, #1907) as the fallback. It proves the startup-gate inversion
// (experiments_enabled off->on->off at runtime), the kill-switch precedence, and the
// ownership invariant (the agent has NO write path to these agent.json flags). The
// per-knob flag-overrides-default / fallback / live-reread behavior is covered in
// flags/flags_test.go (TestExperiment_FlagOverridesCompiledDefault), and the registry /
// verdict live-cap reconfigure in experiment/{registry,verdict}_test.go.

// liveConfig is a goroutine-safe settable ExperimentConfig for the configOverride seam.
// The experiment_seed flag is a separate flag, so it is held apart from the rest
// (seed) and merged into every config whose Seed is unset: a test that sets the
// gates keeps the loop's seed, as a flag flip of another key would.
type liveConfig struct {
	mu   sync.Mutex
	cfg  flags.ExperimentConfig
	seed any
}

// setSeed sets the resolved experiment_seed object (#1907); nil = unresolved.
func (l *liveConfig) setSeed(seed any) {
	l.mu.Lock()
	l.seed = seed
	l.mu.Unlock()
}

func (l *liveConfig) set(c flags.ExperimentConfig) {
	l.mu.Lock()
	l.cfg = c
	l.mu.Unlock()
}

func (l *liveConfig) get(context.Context) flags.ExperimentConfig {
	l.mu.Lock()
	defer l.mu.Unlock()
	cfg := l.cfg
	if cfg.Seed == nil {
		cfg.Seed = l.seed
	}
	return cfg
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
			Tick: time.Hour, DynamicMaxConcurrent: defaultDynamicMaxConcurrent,
		},
		configOverride: lc.get,
		// Kill-switch ON via the seam so experiments_enabled is the variable under test.
		enabledOverride: func(context.Context) bool { return true },
	}
	lc.setSeed(testSeed("nonstop.respawn_seconds", 6.0, 50))
	return loop, spawner, lc
}

// TestExperimentsEnabled_OffOnOff is the startup-gate-inversion AC (#1044): the loop
// is always running, but a runtime off->on flip STARTS it (rehydrate+seed+allocate)
// and an on->off flip STOPS it cleanly (AbortAll strips targeting + frees capacity),
// all with no restart and no in-flight corruption. The off->on->off->on cycle pins
// the #1051 / #1907 seed latch: a re-enable with the seed left on does NOT
// re-declare it, but a full `agent-dryrun-enable.sh off` (gates off, then the
// seed cleared to `off` while the gates are still off) followed by `on` does.
// UC: UC-043 A1
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

	// Flip ON again — the off->on->off->on RE-ENABLE (#1051). The seed value was
	// latched on the FIRST enable, so it is NOT re-declared here: the seed
	// experiment is now terminal (aborted by the on->off teardown) and
	// maybeDeclareSeed skips the latched value (#1907). A re-enable resumes via
	// rehydrate (no non-terminal experiment survives the abort) + the dynamic
	// declarer (unwired in this test). So no fresh seed experiment appears and
	// nothing spawns.
	if !loop.startedOnce {
		t.Fatal("startedOnce must stay latched across off->on->off->on (one-shot bootstrap)")
	}
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
	loop.allocateIfEnabled(ctx)
	if !loop.startedOnce {
		t.Fatal("re-enable cleared startedOnce; the rehydrate bootstrap must remain one-shot")
	}
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("re-enable RE-DECLARED a latched seed value: LiveCount=%d, want 0 (#1051, #1907)", got)
	}
	if recs := spawner.drain(); len(recs) != 0 {
		t.Fatalf("re-enable spawned shadow games off the one-shot seed: %d, want 0 (#1051)", len(recs))
	}

	// `agent-dryrun-enable.sh off`: the gates go off first, then the seed is
	// cleared; the loop sees `off` on a DISABLED tick. Then `on`: the seed is set
	// again before the gates, and the same value is declared again (#1907).
	lc.set(flags.ExperimentConfig{Enabled: false})
	loop.allocateIfEnabled(ctx)
	lc.setSeed(map[string]any{})
	loop.allocateIfEnabled(ctx)
	lc.setSeed(testSeed("nonstop.respawn_seconds", 6.0, 50))
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
	loop.allocateIfEnabled(ctx)
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("off (seed cleared while disabled) -> on did not re-declare the seed: LiveCount=%d, want 1 (#1907)", got)
	}
	if recs := spawner.drain(); len(recs) == 0 {
		t.Fatal("re-declared seed spawned no shadow game")
	}
}

// TestExperimentsEnabled_KillSwitchStillWins asserts the master kill-switch
// (agent.json enabled) still beats experiments_enabled: a disabled agent does
// nothing even with experiments_enabled=true.
// UC: UC-043 A1
// UC: UC-045 main
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
// agent.json + no flag" shape), config() returns the compiled defaults with both
// gates closed, so a fresh deployment keeps experiments OFF (#1907: the removed
// AGENT_EXPERIMENTS_ENABLED cannot open it).
func TestExperimentsEnabled_DefaultSafeFreshConfig(t *testing.T) {
	t.Setenv("AGENT_EXPERIMENTS_ENABLED", "true") // ignored since #1907
	loop, spawner, _ := loopForConfigTest(t, 8)
	loop.configOverride = nil // force the real config() path: nil flags -> compiled defaults
	loop.flags = nil
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
// the agent flag set is human-owned; the agent must NEVER write its own
// experiment-enable flags. The agent's only experiment write path is
// experiment.Writer/Gate, which are hardcoded to the game flag set — so there is
// no path by which the agent could set experiments_enabled on itself, even when
// the flag service would accept a stack write to it.
func TestOwnership_AgentHasNoWritePathToAgentJSON(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	agentPath := filepath.Join(t.TempDir(), "agent.json")
	const agentDoc = `{"metadata":{"flagSetId":"agent"},"flags":{"experiments_enabled":{"state":"ENABLED","variants":{"on":true,"off":false},"defaultVariant":"off"}}}`
	if err := os.WriteFile(agentPath, []byte(agentDoc), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := svc.LoadSet("agent", agentPath, map[string][]string{"experiments_enabled": {"stack", "experiment+arm"}}); err != nil {
		t.Fatal(err)
	}

	log := testLogger()
	gate := experiment.NewGate(experiment.NewWriter(svc, log), nil, log)
	for key, reason := range map[string]experiment.Reason{
		"experiments_enabled":          experiment.ReasonUnknownFlag,   // looked up in the game set only
		"../agent/experiments_enabled": experiment.ReasonNonGameTarget, // a smuggled path
	} {
		err := gate.Review(context.Background(), experiment.Proposal{
			FlagKey:           key,
			ExperimentID:      experiment.MintExperimentID(),
			ExperimentalValue: true,
			Rationale:         "agent tries to enable itself (must be blocked)",
		})
		if rej, ok := experiment.AsReject(err); !ok || rej.Reason != reason {
			t.Fatalf("%s: expected %s, got %v", key, reason, err)
		}
	}
	for _, call := range svc.Calls {
		if strings.Contains(call, "agent/") {
			t.Fatalf("OWNERSHIP VIOLATION: a write reached the agent set: %v", svc.Calls)
		}
	}
}
