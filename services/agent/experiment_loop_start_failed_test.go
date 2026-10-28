package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/promote"
)

// TestExperimentLoop_StartFailed_AbortsExperiment: a declared experiment whose start
// fails (here the flag is not in the experiment catalog, so the invariant gate
// refuses the write) is aborted with start_failed on every declarer path that goes
// through declareAndStart (experiment seed, dynamic declarer, retro thesis). It never stays
// proposed, so it holds neither its flag under test nor a slot of the
// concurrent-experiment budget (D-A21, #1700).
//
// UC: UC-043 A8
func TestExperimentLoop_StartFailed_AbortsExperiment(t *testing.T) {
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	refused := experiment.Intent{FlagKey: "not_an_experiment_flag", ExperimentalValue: 1.0, Objective: "balanced", TargetNPerArm: 3}

	assertFreed := func(t *testing.T, loop *experimentLoop) {
		t.Helper()
		if n := loop.registry.LiveCount(); n != 0 {
			t.Errorf("LiveCount = %d, want 0: a failed start must free its budget slot", n)
		}
		if _, held := loop.registry.ActiveFlags()[refused.FlagKey]; held {
			t.Errorf("ActiveFlags still holds %q: a failed start must free its flag", refused.FlagKey)
		}
	}

	t.Run("UC-043/A8 declare and start (dynamic and retro paths)", func(t *testing.T) {
		loop := buildLoopForTest(t, &recordingSpawner{}, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
		id, err := loop.declareAndStart(context.Background(), refused)
		if err == nil {
			t.Fatal("declareAndStart: want the start error, got nil")
		}
		if st, _ := loop.registry.Status(id); st != experiment.StatusAborted {
			t.Errorf("status = %q, want aborted", st)
		}
		view, ok := loop.registry.CompactView(id)
		if !ok || view.Status != string(experiment.StatusAborted) {
			t.Errorf("journal status = %q, want aborted", view.Status)
		}
		assertFreed(t, loop)
	})

	t.Run("UC-043/A8 experiment seed", func(t *testing.T) {
		loop := buildLoopForTest(t, &recordingSpawner{}, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
		cfg := flags.ExperimentConfig{
			Enabled: true,
			Seed:    testSeed(refused.FlagKey, refused.ExperimentalValue, refused.TargetNPerArm),
		}
		// The seed decision re-reads the flag under the declarer lock (#1966).
		loop.configOverride = func(context.Context) flags.ExperimentConfig { return cfg }
		loop.maybeDeclareSeed(context.Background(), cfg)
		assertFreed(t, loop)
	})
}
