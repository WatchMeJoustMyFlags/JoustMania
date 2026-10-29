package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/promote"
)

// UC-043 Pre-1: shadow experiments run only while both `enabled` (the agent kill
// switch) and `experiments_enabled` are on, and an experiment is declared (Pre-2).
// With either off the tick spawns nothing and aborts the live experiment.
// UC: UC-043 Pre-1 Pre-2
func TestExperimentLoop_RunsOnlyWhenEnabledAndExperimentsEnabled(t *testing.T) {
	cases := []struct {
		name               string
		experimentsEnabled bool
		agentEnabled       bool
		declare            bool
		wantSpawned        bool
		wantLive           bool
	}{
		{"UC-043/Pre-1 both on and an experiment declared: shadow games spawn", true, true, true, true, true},
		{"UC-043/Pre-2 both on but nothing declared: nothing spawns", true, true, false, false, false},
		{"UC-043/Pre-1 experiments_enabled off: nothing spawns, experiment aborted", false, true, true, false, false},
		{"UC-043/Pre-1 agent kill switch off: nothing spawns, experiment aborted", true, false, true, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			spawner := &recordingSpawner{}
			resolve := func(context.Context) promote.Config {
				return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal}
			}
			loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
			loop.configOverride = func(context.Context) flags.ExperimentConfig {
				return flags.ExperimentConfig{Enabled: tc.experimentsEnabled}
			}
			loop.enabledOverride = func(context.Context) bool { return tc.agentEnabled }

			ctx := context.Background()
			if tc.declare {
				id, err := loop.registry.Declare(experiment.Intent{
					FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
				})
				if err != nil {
					t.Fatalf("declare: %v", err)
				}
				if err := loop.registry.Start(ctx, id); err != nil {
					t.Fatalf("start: %v", err)
				}
			}

			loop.allocateIfEnabled(ctx)

			if got := len(spawner.drain()) > 0; got != tc.wantSpawned {
				t.Errorf("spawned = %v, want %v", got, tc.wantSpawned)
			}
			if got := loop.registry.LiveCount() > 0; got != tc.wantLive {
				t.Errorf("live experiment = %v, want %v", got, tc.wantLive)
			}
		})
	}
}
