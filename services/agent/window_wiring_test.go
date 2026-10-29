package main

import (
	"context"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/patterngame"
)

// fakeWindow records what the experiment loop feeds the cross-game window.
type fakeWindow struct {
	mu        sync.Mutex
	patterns  []string
	forgotten []string
}

func (w *fakeWindow) RecordPattern(rec patterngame.Record) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.patterns = append(w.patterns, rec.GameID)
}

func (w *fakeWindow) Forget(id string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.forgotten = append(w.forgotten, id)
}

// UC: UC-042 BR-005 BR-006
func TestPatternEndChain_KeepsRegistryAndForwardsToWindow(t *testing.T) {
	t.Run("UC-042/BR-006 registry first, then window", func(t *testing.T) {
		var calls []string
		chain := patternEndChain(
			func(rec patterngame.Record) { calls = append(calls, "registry:"+rec.GameID) },
			func(rec patterngame.Record) { calls = append(calls, "window:"+rec.GameID) },
		)
		chain(patterngame.Record{Info: patterngame.Info{GameID: "g1"}})
		if len(calls) != 2 || calls[0] != "registry:g1" || calls[1] != "window:g1" {
			t.Errorf("calls = %v, want [registry:g1 window:g1]", calls)
		}
	})
	t.Run("UC-042/BR-006 no window keeps the registry hook", func(t *testing.T) {
		called := false
		chain := patternEndChain(func(patterngame.Record) { called = true }, nil)
		chain(patterngame.Record{})
		if !called {
			t.Error("registry hook not called without a window")
		}
	})
	t.Run("UC-042/BR-006 built loop forwards pattern ends to the window", func(t *testing.T) {
		t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir())
		w := &fakeWindow{}
		loop := buildExperimentLoop(nil, nil, nil, gameServiceForDeclarer(t), w, testLogger())
		if loop.spawner.onPatternEnd == nil || loop.spawner.onPatternStart == nil {
			t.Fatal("pattern callbacks not wired")
		}
		loop.spawner.onPatternEnd(patterngame.Record{Info: patterngame.Info{GameID: "game_p1", ExperimentID: "exp-unknown", PairIndex: -1}})
		w.mu.Lock()
		defer w.mu.Unlock()
		if len(w.patterns) != 1 || w.patterns[0] != "game_p1" {
			t.Errorf("window received %v, want [game_p1]", w.patterns)
		}
	})
	t.Run("UC-042/BR-005 built loop evicts a torn-down experiment's linked set", func(t *testing.T) {
		t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir())
		w := &fakeWindow{}
		loop := buildExperimentLoop(nil, nil, nil, gameServiceForDeclarer(t), w, testLogger())
		id, err := loop.registry.Declare(experiment.Intent{
			FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
		})
		if err != nil {
			t.Fatalf("declare: %v", err)
		}
		if err := loop.registry.Abort(context.Background(), id, "test"); err != nil {
			t.Fatalf("abort: %v", err)
		}
		w.mu.Lock()
		defer w.mu.Unlock()
		if len(w.forgotten) != 1 || w.forgotten[0] != id {
			t.Errorf("window forgot %v, want [%s]", w.forgotten, id)
		}
	})
}
