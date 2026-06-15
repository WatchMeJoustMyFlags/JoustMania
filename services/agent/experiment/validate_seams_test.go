package experiment

import (
	"context"
	"testing"
)

// validate_seams_test.go covers the #965 store-backed Validator seams: the
// StoreBaseline (recent-real baseline), StoreMeasurer (synthetic-run fitness), and
// WriterReverter (degradation rollback).

// TestStoreBaseline_MeanOfRecentReal: the baseline is the mean of the last N real
// games for the proposal's objective.
func TestStoreBaseline_MeanOfRecentReal(t *testing.T) {
	store := NewFitnessStore(0)
	store.Record(FitnessSample{GameID: "r1", Fitness: 0.4, Objective: "endurance", GameKind: GameKindReal})
	store.Record(FitnessSample{GameID: "r2", Fitness: 0.6, Objective: "endurance", GameKind: GameKindReal})
	// A shadow + a different-objective sample must NOT affect the endurance baseline.
	store.Record(FitnessSample{GameID: "sh", Fitness: 0.99, Objective: "endurance", GameKind: "shadow"})
	store.Record(FitnessSample{GameID: "rb", Fitness: 0.1, Objective: "balanced", GameKind: GameKindReal})

	objResolver := func(Proposal) string { return "endurance" }
	b := NewStoreBaseline(store, objResolver, 8, 2)

	got, err := b.Fitness(context.Background(), Proposal{ExperimentID: "exp_1"})
	if err != nil {
		t.Fatalf("Fitness: %v", err)
	}
	if got < 0.49 || got > 0.51 {
		t.Errorf("baseline = %v, want ~0.5 (mean of 0.4,0.6)", got)
	}
}

// TestStoreBaseline_FailsClosedBelowMinSamples: with fewer than minSamples real
// games the baseline fails closed (the Validator must not promote against it).
func TestStoreBaseline_FailsClosedBelowMinSamples(t *testing.T) {
	store := NewFitnessStore(0)
	store.Record(FitnessSample{GameID: "r1", Fitness: 0.4, Objective: "endurance", GameKind: GameKindReal})

	b := NewStoreBaseline(store, func(Proposal) string { return "endurance" }, 8, 3)
	if _, err := b.Fitness(context.Background(), Proposal{}); err == nil {
		t.Fatalf("expected fail-closed error with only 1 real sample (min 3)")
	}
}

// TestStoreBaseline_NilStoreFailsClosed: a nil store fails closed.
func TestStoreBaseline_NilStoreFailsClosed(t *testing.T) {
	b := NewStoreBaseline(nil, nil, 8, 1)
	if _, err := b.Fitness(context.Background(), Proposal{}); err == nil {
		t.Fatalf("expected fail-closed error with nil store")
	}
}

// TestStoreMeasurer_MeanOfRunGames: the measurer averages the run's shadow games'
// finalized fitness, read back by game_id.
func TestStoreMeasurer_MeanOfRunGames(t *testing.T) {
	store := NewFitnessStore(0)
	store.Record(FitnessSample{GameID: "game_a", Fitness: 0.5, GameKind: "shadow"})
	store.Record(FitnessSample{GameID: "game_b", Fitness: 0.7, GameKind: "shadow"})

	m := NewStoreMeasurer(store)
	got, err := m.Measure(context.Background(), NewRun([]string{"game_a", "game_b"}))
	if err != nil {
		t.Fatalf("Measure: %v", err)
	}
	if got < 0.59 || got > 0.61 {
		t.Errorf("measured fitness = %v, want ~0.6", got)
	}
}

// TestStoreMeasurer_PartialBatch: a run whose games partially finalized still
// measures over the games that DID finalize (a smaller honest sample).
func TestStoreMeasurer_PartialBatch(t *testing.T) {
	store := NewFitnessStore(0)
	store.Record(FitnessSample{GameID: "game_a", Fitness: 0.8, GameKind: "shadow"})
	// game_b never finalized.

	m := NewStoreMeasurer(store)
	got, err := m.Measure(context.Background(), NewRun([]string{"game_a", "game_b"}))
	if err != nil {
		t.Fatalf("Measure: %v", err)
	}
	if got != 0.8 {
		t.Errorf("measured fitness = %v, want 0.8 (only game_a finalized)", got)
	}
}

// TestStoreMeasurer_FailsClosedNoneFinalized: a run whose games none finalized
// fails closed (an unmeasurable run never promotes).
func TestStoreMeasurer_FailsClosedNoneFinalized(t *testing.T) {
	m := NewStoreMeasurer(NewFitnessStore(0))
	if _, err := m.Measure(context.Background(), NewRun([]string{"game_x", "game_y"})); err == nil {
		t.Fatalf("expected fail-closed error when no run game finalized")
	}
	// And an empty run.
	if _, err := m.Measure(context.Background(), Run{}); err == nil {
		t.Fatalf("expected fail-closed error on an empty run")
	}
}

// TestWriterReverter_StripsTargeting: Revert strips the experiment's shadow
// targeting branch via the Writer (the inverse of the Gate's Apply).
func TestWriterReverter_StripsTargeting(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	const flag = "invincibility_seconds"
	const expID = "exp_revert1234"
	if err := w.Apply(Proposal{FlagKey: flag, ExperimentID: expID, ExperimentalValue: 6.0}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if _, ok := flagVariants(t, path, flag)[experimentVariantName(expID)]; !ok {
		t.Fatalf("precondition: experiment variant should exist after apply")
	}

	rev := NewWriterReverter(w)
	if err := rev.Revert(context.Background(), Proposal{FlagKey: flag, ExperimentID: expID}); err != nil {
		t.Fatalf("Revert: %v", err)
	}
	if _, ok := flagVariants(t, path, flag)[experimentVariantName(expID)]; ok {
		t.Fatalf("after Revert: experiment variant should be stripped")
	}
}

// TestValidatorEndToEndOverStoreSeams: the M7-7 Validator over the LIVE store seams
// (Baseline + Measurer fed from the store) reaches a PROMOTE verdict when the shadow
// run's fitness beats the recent-real baseline past the threshold — the #965
// end-to-end "validate verdict on real shadow fitness" path with NO fakes for the
// fitness reads.
func TestValidatorEndToEndOverStoreSeams(t *testing.T) {
	store := NewFitnessStore(0)
	// Recent-real baseline ≈ 0.40.
	store.Record(FitnessSample{GameID: "r1", Fitness: 0.40, Objective: "endurance", GameKind: GameKindReal})
	store.Record(FitnessSample{GameID: "r2", Fitness: 0.40, Objective: "endurance", GameKind: GameKindReal})
	store.Record(FitnessSample{GameID: "r3", Fitness: 0.40, Objective: "endurance", GameKind: GameKindReal})
	// The shadow validation batch finalized ≈ 0.70 (a real improvement).
	store.Record(FitnessSample{GameID: "game_s1", Fitness: 0.70, GameKind: "shadow"})
	store.Record(FitnessSample{GameID: "game_s2", Fitness: 0.70, GameKind: "shadow"})

	// A runner that "spawned" the two shadow games (returns their pre-recorded ids).
	runner := stubRunner{run: NewRun([]string{"game_s1", "game_s2"})}
	baseline := NewStoreBaseline(store, func(Proposal) string { return "endurance" }, 8, 3)
	measurer := NewStoreMeasurer(store)

	v, _ := validatorWithRecorder(t, baseline, runner, measurer, nil)
	dec := v.Validate(context.Background(), Proposal{FlagKey: "x", ExperimentID: "exp_e2e"}, defaultCfg())

	if dec.Err != nil {
		t.Fatalf("unexpected err: %v", dec.Err)
	}
	if dec.Outcome != OutcomePromote {
		t.Fatalf("outcome = %q, want promote (0.70 vs 0.40 baseline)", dec.Outcome)
	}
	if dec.FitnessBefore < 0.39 || dec.FitnessBefore > 0.41 {
		t.Errorf("fitness_before = %v, want ~0.40 (recent-real baseline)", dec.FitnessBefore)
	}
	if dec.FitnessAfter < 0.69 || dec.FitnessAfter > 0.71 {
		t.Errorf("fitness_after = %v, want ~0.70 (shadow run mean)", dec.FitnessAfter)
	}
}

// stubRunner is a SyntheticRunner that returns a pre-built Run (the shadow game_ids
// the store already holds), so the end-to-end test exercises the REAL Baseline +
// Measurer over the store without spinning a game stack.
type stubRunner struct{ run Run }

func (s stubRunner) Run(context.Context, Proposal, int) (Run, error) { return s.run, nil }
