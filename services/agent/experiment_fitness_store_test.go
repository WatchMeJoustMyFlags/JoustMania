package main

import (
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamecontext"
)

// experiment_fitness_store_test.go covers the #965/#1017 instrumentation: onGameEnd
// recording finalized per-game fitness into the store for BOTH real and shadow
// games (the previously-DROPPED real-game fitness the Validator's Baseline needs).

// loopWithStore builds a minimal experimentLoop with a real fitness store and the
// default fitness func, for exercising the onGameEnd recording paths in isolation.
// It has NO registry, so the experiment-game branch's ConcludeGame is not driven
// here — these tests assert only the STORE recording, which happens before
// ConcludeGame. (The full conclude pipeline is covered by experiment_loop_test.go.)
func loopWithStore(store *experiment.FitnessStore) *experimentLoop {
	return &experimentLoop{
		log:          testLogger(),
		fitness:      defaultGameFitness,
		fitnessStore: store,
	}
}

// realEnduranceGC builds a finished REAL game whose endurance fitness is evaluable
// (it carries a recovered duration via a start→end timeline). durationS controls the
// session length so the endurance progress is predictable.
func realEnduranceGC(gameID string, durationS float64) gamecontext.GameContext {
	start := time.Unix(1_000, 0)
	return gamecontext.GameContext{
		SessionID: gameID,
		GameKind:  experiment.GameKindReal,
		Timeline: []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "start"},
			{At: start.Add(time.Duration(durationS) * time.Second), Kind: gamecontext.EventPhase, Detail: "end"},
		},
	}
}

// TestOnGameEnd_RecordsRealGameFitness: a finished REAL game's fitness is now
// stored, keyed by game_id, for every experimentable objective it evaluated — the
// #1017 gap (previously dropped). The recorded sample is readable as a recent-real
// baseline input.
func TestOnGameEnd_RecordsRealGameFitness(t *testing.T) {
	store := experiment.NewFitnessStore(0)
	loop := loopWithStore(store)

	// A long real game → high endurance progress.
	loop.onGameEnd(realEnduranceGC("game_real1", 600))

	sample, ok := store.Get("game_real1")
	if !ok {
		t.Fatalf("real game fitness not recorded in the store (#1017 gap)")
	}
	if sample.GameKind != experiment.GameKindReal {
		t.Errorf("recorded game_kind = %q, want real", sample.GameKind)
	}
	// The store must surface it as a recent-real sample for the endurance baseline.
	recent := store.RecentReal(decision.ObjectiveEndurance, 8)
	found := false
	for _, s := range recent {
		if s.GameID == "game_real1" {
			found = true
		}
	}
	if !found {
		t.Fatalf("recorded real game not returned by RecentReal(endurance)")
	}
}

// TestOnGameEnd_ShadowWithoutExperimentNotInRealBaseline: a game labeled shadow but
// carrying no ExperimentID must NOT pollute the recent-real baseline (only real
// games seed it).
func TestOnGameEnd_ShadowWithoutExperimentNotInRealBaseline(t *testing.T) {
	store := experiment.NewFitnessStore(0)
	loop := loopWithStore(store)

	gc := realEnduranceGC("game_shadowish", 600)
	gc.GameKind = "shadow" // labeled shadow, no ExperimentID
	loop.onGameEnd(gc)

	if got := store.RecentReal(decision.ObjectiveEndurance, 8); len(got) != 0 {
		t.Fatalf("a shadow-labeled game must not seed the real baseline, got %d samples", len(got))
	}
}

// TestRecordFinalizedRealFitness_SkipsMissingSignalObjective: a real game whose
// signals don't support an objective (e.g. no duration → endurance skipped) does NOT
// fabricate a sample for it (the #731 honesty contract).
func TestRecordFinalizedRealFitness_SkipsMissingSignalObjective(t *testing.T) {
	store := experiment.NewFitnessStore(0)
	loop := loopWithStore(store)

	// No duration, no timeline → endurance/accelerate skip; no players → balanced skips.
	gc := gamecontext.GameContext{SessionID: "game_signalless", GameKind: experiment.GameKindReal}
	loop.onGameEnd(gc)

	if store.Len() != 0 {
		t.Fatalf("a signal-less real game must not fabricate a fitness sample, got %d", store.Len())
	}
}
