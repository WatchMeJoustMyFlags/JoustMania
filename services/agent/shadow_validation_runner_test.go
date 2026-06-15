package main

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
)

// shadow_validation_runner_test.go covers the #965 live SyntheticRunner: it spawns
// real shadow games via the spawner seam and awaits their finalized fitness in the
// store. The spawner is a recording fake (the real shadowSpawner needs a game
// stack); the store is real.

// validationSpawner is a fake experiment.ShadowSpawner for the runner tests: it
// mints sequential game_ids and records the (experimentID, arm) of each call. Named
// distinctly from the experiment_loop_test recordingSpawner to avoid a redeclare.
type validationSpawner struct {
	calls          int
	ids            []string
	gotExperiments []string
	gotArms        []string
}

func (s *validationSpawner) Spawn(_ context.Context, experimentID, arm string, _ uint64) (string, error) {
	s.calls++
	s.gotExperiments = append(s.gotExperiments, experimentID)
	s.gotArms = append(s.gotArms, arm)
	id := "game_s" + string(rune('0'+len(s.ids)))
	s.ids = append(s.ids, id)
	return id, nil
}

// alwaysErrSpawner errors on every Spawn (no game ever starts).
type alwaysErrSpawner struct{}

func (alwaysErrSpawner) Spawn(context.Context, string, string, uint64) (string, error) {
	return "", errors.New("coordinator unreachable")
}

// newTestRunner builds a runner with a fake spawner + a real store and a fast,
// no-real-sleep poll so the await loop turns quickly in tests.
func newTestRunner(spawner experiment.ShadowSpawner, store *experiment.FitnessStore) *shadowValidationRunner {
	r := newShadowValidationRunner(spawner, store, 2*time.Second, discardLogger())
	r.pollInterval = time.Millisecond
	r.sleep = func(ctx context.Context, _ time.Duration) error {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
			return nil
		}
	}
	return r
}

// TestRunner_SpawnsExperimentalArmAndReturnsFinalizedIDs: the runner spawns N games
// bound to the proposal's experiment + the EXPERIMENTAL arm and returns a Run with
// the game_ids whose fitness finalized in the store.
func TestRunner_SpawnsExperimentalArmAndReturnsFinalizedIDs(t *testing.T) {
	spawner := &validationSpawner{}
	store := experiment.NewFitnessStore(0)
	r := newTestRunner(spawner, store)

	// Pre-seed the store so the spawned games (game_s0, game_s1) are "already
	// finalized" the moment the runner awaits.
	store.Record(experiment.FitnessSample{GameID: "game_s0", Fitness: 0.6, GameKind: "shadow"})
	store.Record(experiment.FitnessSample{GameID: "game_s1", Fitness: 0.6, GameKind: "shadow"})

	run, err := r.Run(context.Background(), experiment.Proposal{ExperimentID: "exp_1"}, 2)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if spawner.calls != 2 {
		t.Errorf("spawner called %d times, want 2", spawner.calls)
	}
	for _, arm := range spawner.gotArms {
		if arm != experiment.ArmExperimental {
			t.Errorf("spawned arm = %q, want experimental", arm)
		}
	}
	for _, exp := range spawner.gotExperiments {
		if exp != "exp_1" {
			t.Errorf("spawned experiment = %q, want exp_1", exp)
		}
	}
	if ids := experiment.RunGameIDs(run); len(ids) != 2 {
		t.Fatalf("run returned %d game ids, want 2", len(ids))
	}
}

// TestRunner_FailsClosedWithoutExperimentID: a proposal with no experiment id is
// refused (the spawned games would resolve the default variant, not the candidate).
func TestRunner_FailsClosedWithoutExperimentID(t *testing.T) {
	r := newTestRunner(&validationSpawner{}, experiment.NewFitnessStore(0))
	if _, err := r.Run(context.Background(), experiment.Proposal{}, 2); err == nil {
		t.Fatalf("expected error for a proposal with no experiment id")
	}
}

// TestRunner_FailsClosedWhenNoGameSpawns: when the spawner cannot start any game,
// Run errors (no evidence → the Validator discards).
func TestRunner_FailsClosedWhenNoGameSpawns(t *testing.T) {
	r := newTestRunner(alwaysErrSpawner{}, experiment.NewFitnessStore(0))
	if _, err := r.Run(context.Background(), experiment.Proposal{ExperimentID: "exp_1"}, 2); err == nil {
		t.Fatalf("expected error when no shadow game could be spawned")
	}
}

// TestRunner_TimeoutReturnsFinalizedSoFar: when some spawned games never finalize,
// the runner returns the ids that DID finalize once the timeout elapses (a smaller
// but valid sample), rather than blocking forever.
func TestRunner_TimeoutReturnsFinalizedSoFar(t *testing.T) {
	spawner := &validationSpawner{}
	store := experiment.NewFitnessStore(0)
	r := newTestRunner(spawner, store)
	// Only game_s0 ever finalizes; drive now() past the deadline after a couple polls.
	store.Record(experiment.FitnessSample{GameID: "game_s0", Fitness: 0.5, GameKind: "shadow"})
	calls := 0
	start := time.Now()
	r.now = func() time.Time {
		calls++
		if calls > 2 {
			return start.Add(time.Hour)
		}
		return start
	}

	run, err := r.Run(context.Background(), experiment.Proposal{ExperimentID: "exp_1"}, 2)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	ids := experiment.RunGameIDs(run)
	if len(ids) != 1 || ids[0] != "game_s0" {
		t.Fatalf("run ids = %v, want [game_s0] (only finalized game)", ids)
	}
}
