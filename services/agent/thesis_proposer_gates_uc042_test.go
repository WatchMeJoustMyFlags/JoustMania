package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// With AGENT_EXPERIMENT_PROPOSER_ENABLED=true and a recorded retro suggestion, the
// thesis proposer stays subordinate to the loop's gates: it declares only on a tick
// where both `enabled` and `experiments_enabled` are on. Driven through the gated
// tick (allocateIfEnabled), not the hook, so the gate is part of what is tested.
// UC: UC-042 A3
func TestRetroProposer_SubordinateToGatesOnTick(t *testing.T) {
	cases := []struct {
		name         string
		experiments  bool
		agentEnabled bool
		wantLive     int
	}{
		{"both on declares", true, true, 1},
		{"experiments_enabled off", false, true, 0},
		{"enabled off", true, false, 0},
	}
	for _, tc := range cases {
		t.Run("UC-042/A3 "+tc.name, func(t *testing.T) {
			t.Setenv(envProposerEnabled, "true")
			loop := proposerLoop(t)
			loop.proposer.Record(experiment.RetroSuggestion{
				InterventionType: "adjust_global_difficulty", Emphasis: "weight_up", Reason: "room faded",
			})
			loop.configOverride = func(context.Context) flags.ExperimentConfig {
				cfg := dynCfg(5)
				cfg.Enabled = tc.experiments
				cfg.DynamicEnabled = false // isolate the retro-seeded path from the dynamic declarer
				return cfg
			}
			loop.enabledOverride = func(context.Context) bool { return tc.agentEnabled }

			loop.allocateIfEnabled(context.Background())

			if got := loop.registry.LiveCount(); got != tc.wantLive {
				t.Fatalf("LiveCount = %d, want %d", got, tc.wantLive)
			}
			if tc.wantLive == 0 {
				if got := loop.proposer.Backlog(); got != 1 {
					t.Errorf("backlog = %d, want 1 (suggestion untouched while gated)", got)
				}
			}
		})
	}
}
