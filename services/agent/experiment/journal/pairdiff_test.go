package journal

import (
	"math"
	"testing"
	"time"
)

// pairdiff_test.go covers the Common-Random-Numbers paired-difference fold (#1004):
// apply()'s pending-pairs buffer that turns out-of-order, cross-tick (experimental,
// control) conclusions into per-pair differences folded into Summary.PairDiff, the
// half-pair drop on release, and the rehydration (replay-on-Load) guarantee.

// pairEvent builds a game_concluded event carrying a CRN pair index (#1003).
func pairEvent(arm string, pairIndex int, fitness float64, at time.Time) Event {
	f := fitness
	pi := pairIndex
	return Event{
		At:        at,
		Kind:      KindGameConcluded,
		GameID:    "game_" + arm,
		Arm:       arm,
		PairIndex: &pi,
		Fitness:   &f,
	}
}

func releaseEvent(arm string, pairIndex int, at time.Time) Event {
	pi := pairIndex
	return Event{
		At:        at,
		Kind:      KindGameReleased,
		GameID:    "game_" + arm + "_released",
		Arm:       arm,
		PairIndex: &pi,
		Note:      "test release",
	}
}

func mustAppend(t *testing.T, j *Journal, e Event) {
	t.Helper()
	if err := j.AppendEvent(e); err != nil {
		t.Fatalf("AppendEvent(%s): %v", e.Kind, e)
	}
}

func at(sec int) time.Time {
	return time.Date(2026, 6, 14, 10, 0, sec, 0, time.UTC)
}

// TestPairDiff_FoldsCompletedPair: a matched (experimental, control) pair folds a
// single d = f_exp − f_ctl into PairDiff and drains the pending buffer.
func TestPairDiff_FoldsCompletedPair(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.80, at(1)))
	// First half parked, no PairDiff yet.
	s := j.Summary()
	if s.PairDiff != nil {
		t.Fatalf("PairDiff should be nil after first half, got %+v", s.PairDiff)
	}
	if got := len(s.PendingPairs); got != 1 {
		t.Fatalf("PendingPairs len = %d, want 1", got)
	}

	mustAppend(t, j, pairEvent(ArmControl, 0, 0.50, at(2)))
	s = j.Summary()
	if s.PairDiff == nil || s.PairDiff.Count != 1 {
		t.Fatalf("PairDiff after completed pair = %+v, want Count=1", s.PairDiff)
	}
	if math.Abs(s.PairDiff.Mean-0.30) > 1e-9 {
		t.Errorf("PairDiff.Mean = %v, want 0.30 (0.80-0.50)", s.PairDiff.Mean)
	}
	if len(s.PendingPairs) != 0 {
		t.Errorf("PendingPairs should be drained, got %+v", s.PendingPairs)
	}
}

// TestPairDiff_OutOfOrderAndCrossTick: arms conclude in either order across ticks
// and still pair correctly; each completed pair folds exactly once.
func TestPairDiff_OutOfOrderAndCrossTick(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	// Pair 0: control concludes FIRST (out of order). Pair 1: experimental first.
	// Interleave two open pairs across ticks before either completes.
	mustAppend(t, j, pairEvent(ArmControl, 0, 0.40, at(1)))      // park ctl@0
	mustAppend(t, j, pairEvent(ArmExperimental, 1, 0.90, at(2))) // park exp@1
	if got := len(j.Summary().PendingPairs); got != 2 {
		t.Fatalf("two open pairs, PendingPairs len = %d, want 2", got)
	}
	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.70, at(3))) // complete pair 0: 0.70-0.40=0.30
	mustAppend(t, j, pairEvent(ArmControl, 1, 0.50, at(4)))      // complete pair 1: 0.90-0.50=0.40

	s := j.Summary()
	if s.PairDiff == nil || s.PairDiff.Count != 2 {
		t.Fatalf("PairDiff = %+v, want Count=2", s.PairDiff)
	}
	// mean of {0.30, 0.40} = 0.35
	if math.Abs(s.PairDiff.Mean-0.35) > 1e-9 {
		t.Errorf("PairDiff.Mean = %v, want 0.35", s.PairDiff.Mean)
	}
	if len(s.PendingPairs) != 0 {
		t.Errorf("PendingPairs should be drained, got %+v", s.PendingPairs)
	}
}

// TestPairDiff_HalfPairDroppedOnRelease: a parked half whose partner is RELEASED
// (never concludes) is dropped — never folded as a fabricated difference (#1004
// carried-over correctness item 1).
func TestPairDiff_HalfPairDroppedOnRelease(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.80, at(1))) // park exp@0
	if len(j.Summary().PendingPairs) != 1 {
		t.Fatalf("expected one parked half")
	}
	// The experimental half's own game is released (its partner control never spawned
	// / it errored). Drop the parked half.
	rel := releaseEvent(ArmExperimental, 0, at(2))
	mustAppend(t, j, rel)

	s := j.Summary()
	if s.PairDiff != nil {
		t.Errorf("PairDiff must stay nil after a dropped half, got %+v", s.PairDiff)
	}
	if len(s.PendingPairs) != 0 {
		t.Errorf("dropped half must drain PendingPairs, got %+v", s.PendingPairs)
	}
}

// TestPairDiff_ReleaseOtherArmLeavesParkedHalf: a release whose arm does NOT match
// the parked half (e.g. a partner re-spawn that itself errors) leaves the parked
// half intact so a genuine later partner can still match.
func TestPairDiff_ReleaseOtherArmLeavesParkedHalf(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.80, at(1))) // park exp@0
	mustAppend(t, j, releaseEvent(ArmControl, 0, at(2)))         // a control release; arm mismatch
	if len(j.Summary().PendingPairs) != 1 {
		t.Fatalf("parked exp half must remain after a mismatched-arm release")
	}
	// A genuine control conclusion still completes the pair.
	mustAppend(t, j, pairEvent(ArmControl, 0, 0.50, at(3)))
	s := j.Summary()
	if s.PairDiff == nil || s.PairDiff.Count != 1 {
		t.Fatalf("pair should complete, PairDiff = %+v", s.PairDiff)
	}
}

// TestPairDiff_ReplayReproducesState: the rehydrated summary (Load replaying the
// event log) reproduces PairDiff AND the residual PendingPairs exactly — the #831
// durability guarantee for the paired state.
func TestPairDiff_ReplayReproducesState(t *testing.T) {
	dir := t.TempDir()
	j, err := Create(dir, testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	// Two completed pairs + one dangling half left pending.
	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.80, at(1)))
	mustAppend(t, j, pairEvent(ArmControl, 0, 0.50, at(2)))
	mustAppend(t, j, pairEvent(ArmControl, 1, 0.30, at(3)))
	mustAppend(t, j, pairEvent(ArmExperimental, 1, 0.60, at(4)))
	mustAppend(t, j, pairEvent(ArmExperimental, 2, 0.55, at(5))) // dangling half

	live := j.Summary()

	reloaded, err := Load(dir, testIntent().ExperimentID)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	got := reloaded.Summary()

	if live.PairDiff == nil || got.PairDiff == nil {
		t.Fatalf("PairDiff nil: live=%+v got=%+v", live.PairDiff, got.PairDiff)
	}
	if *live.PairDiff != *got.PairDiff {
		t.Errorf("PairDiff differs after replay: live=%+v got=%+v", *live.PairDiff, *got.PairDiff)
	}
	if len(got.PendingPairs) != 1 {
		t.Errorf("residual PendingPairs len after replay = %d, want 1", len(got.PendingPairs))
	}
	if got.PendingPairs[2] != live.PendingPairs[2] {
		t.Errorf("residual pending half differs: live=%+v got=%+v",
			live.PendingPairs[2], got.PendingPairs[2])
	}
}

// TestPairDiff_NoPairIndexIgnored: a conclusion WITHOUT a pair index (legacy /
// unpaired) folds only into the per-arm Welford and never creates a PairDiff.
func TestPairDiff_NoPairIndexIgnored(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	mustAppend(t, j, fitnessEvent(ArmExperimental, 0.8, at(1)))
	mustAppend(t, j, fitnessEvent(ArmControl, 0.5, at(2)))
	s := j.Summary()
	if s.PairDiff != nil {
		t.Errorf("unpaired conclusions must not create PairDiff, got %+v", s.PairDiff)
	}
	if len(s.PendingPairs) != 0 {
		t.Errorf("unpaired conclusions must not park pending halves, got %+v", s.PendingPairs)
	}
	if s.Arms[ArmExperimental].Count != 1 || s.Arms[ArmControl].Count != 1 {
		t.Errorf("per-arm Welford should still fold unpaired samples")
	}
}

// TestPairDiff_SameArmRepeatDoesNotFold: a defensive case — two conclusions for the
// SAME arm at one pair index must not fold a same-arm "difference".
func TestPairDiff_SameArmRepeatDoesNotFold(t *testing.T) {
	j, err := Create(t.TempDir(), testIntent())
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.80, at(1)))
	mustAppend(t, j, pairEvent(ArmExperimental, 0, 0.60, at(2))) // same arm, same pair
	s := j.Summary()
	if s.PairDiff != nil {
		t.Errorf("same-arm repeat must not fold a PairDiff, got %+v", s.PairDiff)
	}
	// The parked half is refreshed to the latest same-arm conclusion.
	if got, ok := s.PendingPairs[0]; !ok || got.Arm != ArmExperimental || math.Abs(got.Fitness-0.60) > 1e-9 {
		t.Errorf("parked half = %+v, want experimental/0.60", got)
	}
}
