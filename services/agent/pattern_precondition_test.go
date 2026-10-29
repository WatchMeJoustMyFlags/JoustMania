package main

import (
	"context"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/patterngame"
)

// countingPinner counts Pin calls and returns one valid archetype.
type countingPinner struct {
	mu    sync.Mutex
	calls int
	pin   patterngame.Pin
}

func (c *countingPinner) Pin(context.Context) patterngame.Pin {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.calls++
	return c.pin
}

func (c *countingPinner) count() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.calls
}

// patternRecordingSpawner records whether each spawn carried a pattern.
type patternRecordingSpawner struct {
	recordingSpawner
	mu       sync.Mutex
	patterns int
}

func (p *patternRecordingSpawner) Spawn(ctx context.Context, experimentID, arm string, seed uint64) (string, error) {
	if _, ok := experiment.PatternSpawnFrom(ctx); ok {
		p.mu.Lock()
		p.patterns++
		p.mu.Unlock()
	}
	return p.recordingSpawner.Spawn(ctx, experimentID, arm, seed)
}

func (p *patternRecordingSpawner) patternCount() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.patterns
}

// UC: UC-046 Pre-1
func TestPatternGames_NoSpawnWhileExperimentsOff(t *testing.T) {
	arch, errs := patterngame.NewArchetype("only", map[string]any{
		"players": 4.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
		"period_s": 3.0, "phase": 0.0, "burst_h": 0.0, "burst_period_s": 0.0,
		"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
	})
	if len(errs) > 0 {
		t.Fatalf("archetype: %v", errs)
	}
	pinner := &countingPinner{pin: patterngame.Pin{Selection: "only", MaxGameSeconds: 300, Archetypes: []patterngame.Archetype{arch}}}
	spawner := &patternRecordingSpawner{}

	log := testLogger()
	writer := experiment.NewWriter(newGameFileForLoop(t), log)
	targeting := experiment.NewGateTargetingWriter(experiment.NewGate(writer, nil, log), writer)
	reg := experiment.NewRegistry(experiment.RegistryConfig{
		Root: t.TempDir(), MaxShadowGames: 2, EffectiveConcurrency: 2, Spawner: spawner,
		Verdict: experiment.NewVerdict(3, 0.5, nil), Targeting: targeting, Log: log, Patterns: pinner,
	})
	lc := &liveConfig{}
	lc.set(flags.ExperimentConfig{Enabled: false})
	loop := &experimentLoop{
		registry:        reg,
		log:             log,
		expDefaults:     flags.ExperimentDefaults{Tick: 0, DynamicMaxConcurrent: defaultDynamicMaxConcurrent},
		configOverride:  lc.get,
		enabledOverride: func(context.Context) bool { return true },
	}
	lc.setSeed(testSeed("nonstop.respawn_seconds", 6.0, 50))
	ctx := context.Background()

	t.Run("UC-046/Pre-1 experiments off: no pattern is pinned and no game spawns", func(t *testing.T) {
		loop.allocateIfEnabled(ctx)
		if n := pinner.count(); n != 0 {
			t.Errorf("archetypes pinned while experiments are off: %d, want 0", n)
		}
		if n := len(spawner.drain()); n != 0 {
			t.Errorf("games spawned while experiments are off: %d, want 0", n)
		}
		if n := reg.LiveCount(); n != 0 {
			t.Errorf("live experiments while off: %d, want 0", n)
		}
	})

	t.Run("UC-046/Pre-1 control: experiments on spawns pattern games", func(t *testing.T) {
		lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 2})
		loop.allocateIfEnabled(ctx)
		if pinner.count() == 0 || spawner.patternCount() == 0 {
			t.Errorf("experiments on spawned no pattern game: pins=%d pattern spawns=%d", pinner.count(), spawner.patternCount())
		}
	})
}
