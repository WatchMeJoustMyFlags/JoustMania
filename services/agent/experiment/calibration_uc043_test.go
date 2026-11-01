package experiment

import (
	"context"
	"encoding/json"
	"math"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/telemetry/keys"
)

// UC: UC-043 A21 BR-010
// calibration_uc043_test.go: arm calibration (#2479, #2498). The Book's per-game
// tally lands on the game's arm, shows on experiment.progress's experiment.arm
// events, and is never journaled and never read by the verdict.

func TestArmCalibration_TalliedPerArmOnProgress(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	r.AllocateAndSpawn(context.Background()) // one game per arm
	if len(spawner.calls) != 2 {
		t.Fatalf("spawned = %d, want 2", len(spawner.calls))
	}
	g0, g1 := spawner.calls[0], spawner.calls[1]
	if g0.arm == g1.arm {
		t.Fatalf("both games on arm %q", g0.arm)
	}

	t.Run("UC-043/A21 a game's tally lands on its own arm and adds up", func(t *testing.T) {
		r.NotePredictions(g0.gameID, 2, 1, 0.5)
		r.NotePredictions(g0.gameID, 1, 1, 0.04)
		r.NotePredictions(g1.gameID, 3, 0, 1.2)
		got := r.ArmCalibration(id)
		want0 := PredictionTally{Scored: 3, Hits: 2, BrierSum: 0.54}
		if c := got[g0.arm]; c.Scored != want0.Scored || c.Hits != want0.Hits || math.Abs(c.BrierSum-want0.BrierSum) > 1e-9 {
			t.Errorf("arm %s = %+v, want %+v", g0.arm, c, want0)
		}
		if c := got[g1.arm]; c != (PredictionTally{Scored: 3, Hits: 0, BrierSum: 1.2}) {
			t.Errorf("arm %s = %+v", g1.arm, c)
		}
	})
	t.Run("UC-043/A21 a game that is not ours and an inconsistent tally are ignored", func(t *testing.T) {
		before := r.ArmCalibration(id)
		r.NotePredictions("real_game", 5, 5, 0)
		r.NotePredictions(g0.gameID, 0, 0, 0)          // empty
		r.NotePredictions(g0.gameID, 1, 2, 0)          // hits > scored
		r.NotePredictions(g0.gameID, 1, 1, 2)          // Brier sum > scored
		r.NotePredictions(g0.gameID, 1, 1, math.NaN()) // not a number
		r.NotePredictions(g0.gameID, 1, 1, -0.1)       // negative
		if got := r.ArmCalibration(id); !reflect.DeepEqual(got, before) {
			t.Errorf("tally changed: %+v -> %+v", before, got)
		}
	})
	t.Run("UC-043/A21 experiment.progress carries predictions_scored, prediction_hits and the mean Brier", func(t *testing.T) {
		if _, err := r.ConcludeGame(context.Background(), g0.gameID, 0.4, 60); err != nil {
			t.Fatal(err)
		}
		spans := progressSpans(sr)
		last := spans[len(spans)-1]
		if v, _ := rootSpanAttr(last, AttrExperimentProgressTrigger); v != ProgressTriggerGameConcluded {
			t.Fatalf("last trigger = %q", v)
		}
		events := map[string]map[string]any{}
		for _, ev := range last.Events() {
			if ev.Name != EventExperimentArm {
				continue
			}
			m := map[string]any{}
			for _, kv := range ev.Attributes {
				m[string(kv.Key)] = kv.Value.AsInterface()
			}
			events[m[keys.AttrExperimentArm].(string)] = m
		}
		e0, e1 := events[g0.arm], events[g1.arm]
		if e0[AttrExperimentArmPredictionsScored] != int64(3) || e0[AttrExperimentArmPredictionHits] != int64(2) {
			t.Errorf("arm %s event = %v", g0.arm, e0)
		}
		if b, _ := e0[AttrExperimentArmPredictionBrier].(float64); math.Abs(b-0.18) > 1e-9 {
			t.Errorf("arm %s mean Brier = %v, want 0.18", g0.arm, e0[AttrExperimentArmPredictionBrier])
		}
		if e1[AttrExperimentArmPredictionsScored] != int64(3) || e1[AttrExperimentArmPredictionHits] != int64(0) {
			t.Errorf("arm %s event = %v", g1.arm, e1)
		}
	})
	t.Run("UC-043/A21 a concluded game is no longer bound: a late tally is ignored", func(t *testing.T) {
		before := r.ArmCalibration(id)
		r.NotePredictions(g0.gameID, 4, 4, 0)
		if got := r.ArmCalibration(id); !reflect.DeepEqual(got, before) {
			t.Errorf("tally changed after conclusion: %+v -> %+v", before, got)
		}
	})
}

// TestArmCalibration_EmptyArmHasNoBrier: an arm without a scored prediction
// reports 0 scored and no Brier (0 would read as a perfect score).
func TestArmCalibration_EmptyArmHasNoBrier(t *testing.T) {
	spawner := &fakeSpawner{}
	r, sr := rootSpanRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	startedExperiment(t, r)
	spans := progressSpans(sr)
	for _, ev := range spans[len(spans)-1].Events() {
		if ev.Name != EventExperimentArm {
			continue
		}
		for _, kv := range ev.Attributes {
			switch string(kv.Key) {
			case AttrExperimentArmPredictionBrier:
				t.Errorf("UC-043/A21 an empty arm must not carry %s", kv.Key)
			case AttrExperimentArmPredictionsScored:
				if kv.Value.AsInt64() != 0 {
					t.Errorf("UC-043/A21 empty arm predictions_scored = %d", kv.Value.AsInt64())
				}
			}
		}
	}
}

// recordingVerdict records every Summary it is asked about and promotes once
// every arm has n samples.
type recordingVerdict struct {
	mu   sync.Mutex
	n    int
	seen []journal.Summary
}

func (v *recordingVerdict) Evaluate(s journal.Summary) (journal.Verdict, bool) {
	v.mu.Lock()
	v.seen = append(v.seen, s)
	v.mu.Unlock()
	for _, a := range s.Arms {
		if a.Count < v.n {
			return journal.Verdict{Outcome: OutcomeInconclusive, Reason: "under-powered"}, true
		}
	}
	if len(s.Arms) < 2 {
		return journal.Verdict{Outcome: OutcomeInconclusive, Reason: "under-powered"}, true
	}
	return journal.Verdict{Outcome: CohortOutcomePromote, Significant: true, Delta: 0.1}, true
}

// calibrationRun runs one experiment to a promote verdict; withTally feeds
// extreme prediction tallies for every game before it concludes.
type calibrationRun struct {
	status   Status
	promoted int
	seen     []journal.Summary
	events   []string
}

func runToPromotion(t *testing.T, withTally bool) calibrationRun {
	t.Helper()
	spawner := &fakeSpawner{}
	verdict := &recordingVerdict{n: 2}
	promoter := &fakePromoter{}
	r := newTestRegistry(t, RegistryConfig{
		Spawner: spawner, Verdict: verdict, Promoter: promoter, Targeting: &fakeTargeting{},
		MaxShadowGames: 2,
	})
	id := startedExperiment(t, r)
	fitness := map[int]float64{0: 0.7, 1: 0.3, 2: 0.8, 3: 0.2}
	for i := 0; i < 4; i++ {
		if i%2 == 0 {
			r.AllocateAndSpawn(context.Background())
		}
		g := spawner.calls[i]
		if withTally {
			// The worst possible calibration on one arm, the best on the other.
			if i%2 == 0 {
				r.NotePredictions(g.gameID, 9, 0, 9)
			} else {
				r.NotePredictions(g.gameID, 9, 9, 0)
			}
		}
		if _, err := r.ConcludeGame(context.Background(), g.gameID, fitness[i], 60); err != nil {
			t.Fatal(err)
		}
	}
	run := calibrationRun{promoted: promoter.count()}
	// The experiment id differs between the two runs; nothing else may.
	for _, s := range verdict.seen {
		s.ExperimentID = ""
		run.seen = append(run.seen, s)
	}
	run.status, _ = r.Status(id)
	r.mu.Lock()
	jrnl := r.entries[id].journal
	r.mu.Unlock()
	evs, err := jrnl.Events()
	if err != nil {
		t.Fatal(err)
	}
	for _, e := range evs {
		e.At = e.At.UTC().Truncate(0)
		raw, _ := json.Marshal(e)
		run.events = append(run.events, strings.ReplaceAll(string(raw), id, "<id>"))
	}
	return run
}

func TestArmCalibration_NeverDecides(t *testing.T) {
	with, without := runToPromotion(t, true), runToPromotion(t, false)
	t.Run("UC-043/BR-010 the comparison is not vacuous", func(t *testing.T) {
		if without.promoted != 1 || len(without.seen) == 0 {
			t.Fatalf("baseline promoted %d times after %d verdicts, want a promotion", without.promoted, len(without.seen))
		}
	})
	t.Run("UC-043/BR-010 the verdict sees the same summaries", func(t *testing.T) {
		if !reflect.DeepEqual(with.seen, without.seen) {
			t.Errorf("verdict input differs with a tally:\nwith    %+v\nwithout %+v", with.seen, without.seen)
		}
	})
	t.Run("UC-043/BR-010 the same conclusion and promotion", func(t *testing.T) {
		if with.status != without.status || with.promoted != without.promoted {
			t.Errorf("with a tally: %s / %d promotions; without: %s / %d", with.status, with.promoted, without.status, without.promoted)
		}
	})
	t.Run("UC-043/A21 the tally is never journaled", func(t *testing.T) {
		if len(with.events) != len(without.events) {
			t.Fatalf("journal has %d events with a tally, %d without", len(with.events), len(without.events))
		}
		for i := range with.events {
			if with.events[i] != without.events[i] {
				t.Errorf("journal event %d differs:\nwith    %s\nwithout %s", i, with.events[i], without.events[i])
			}
		}
	})
}
