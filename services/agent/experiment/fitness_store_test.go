package experiment

import (
	"testing"
	"time"
)

// fitness_store_test.go covers the #965 finalized-per-game fitness store: the
// instrumentation that turns the Validator's injected fitness reads into real ones.

// TestFitnessStore_RecordAndGet: a recorded sample is read back by game_id.
func TestFitnessStore_RecordAndGet(t *testing.T) {
	s := NewFitnessStore(0) // default capacity
	s.Record(FitnessSample{GameID: "game_aaa", Fitness: 0.7, Objective: "endurance", GameKind: "shadow"})

	got, ok := s.Get("game_aaa")
	if !ok {
		t.Fatalf("Get(game_aaa) not found after Record")
	}
	if got.Fitness != 0.7 {
		t.Errorf("fitness = %v, want 0.7", got.Fitness)
	}
	if _, ok := s.Get("game_missing"); ok {
		t.Errorf("Get(game_missing) should be absent")
	}
}

// TestFitnessStore_EmptyGameIDDropped: a sample with no game_id is not stored
// (nothing could ever read it back).
func TestFitnessStore_EmptyGameIDDropped(t *testing.T) {
	s := NewFitnessStore(0)
	s.Record(FitnessSample{GameID: "", Fitness: 0.5, GameKind: "real"})
	if s.Len() != 0 {
		t.Fatalf("empty-game-id sample should be dropped, len = %d", s.Len())
	}
}

// TestFitnessStore_RingEviction: the ring evicts the oldest sample once capacity is
// exceeded, keeping memory bounded.
func TestFitnessStore_RingEviction(t *testing.T) {
	s := NewFitnessStore(2)
	s.Record(FitnessSample{GameID: "g1", Fitness: 0.1, GameKind: "real"})
	s.Record(FitnessSample{GameID: "g2", Fitness: 0.2, GameKind: "real"})
	s.Record(FitnessSample{GameID: "g3", Fitness: 0.3, GameKind: "real"}) // evicts g1

	if s.Len() != 2 {
		t.Fatalf("len = %d, want 2 (ring capacity)", s.Len())
	}
	if _, ok := s.Get("g1"); ok {
		t.Errorf("g1 should have been evicted (oldest)")
	}
	if _, ok := s.Get("g3"); !ok {
		t.Errorf("g3 (newest) should be present")
	}
}

// TestFitnessStore_ReRecordKeepsBounded: re-Recording the same game_id updates in
// place without growing the order ring (so a duplicate cannot evict live samples).
func TestFitnessStore_ReRecordKeepsBounded(t *testing.T) {
	s := NewFitnessStore(2)
	s.Record(FitnessSample{GameID: "g1", Fitness: 0.1, GameKind: "real"})
	s.Record(FitnessSample{GameID: "g2", Fitness: 0.2, GameKind: "real"})
	s.Record(FitnessSample{GameID: "g1", Fitness: 0.9, GameKind: "real"}) // update, not new

	if s.Len() != 2 {
		t.Fatalf("len = %d, want 2 after re-record", s.Len())
	}
	got, _ := s.Get("g1")
	if got.Fitness != 0.9 {
		t.Errorf("re-record fitness = %v, want 0.9", got.Fitness)
	}
	if _, ok := s.Get("g2"); !ok {
		t.Errorf("g2 must not be evicted by a re-record of g1")
	}
}

// TestFitnessStore_RecentRealFiltersKindAndObjective: RecentReal returns only
// game_kind="real" samples for the requested objective, newest-first, up to n.
func TestFitnessStore_RecentRealFiltersKindAndObjective(t *testing.T) {
	s := NewFitnessStore(0)
	base := time.Now()
	// Two real endurance samples, one real balanced, one shadow endurance.
	s.Record(FitnessSample{GameID: "r1", Fitness: 0.4, Objective: "endurance", GameKind: "real", At: base})
	s.Record(FitnessSample{GameID: "r2", Fitness: 0.6, Objective: "endurance", GameKind: "real", At: base.Add(time.Second)})
	s.Record(FitnessSample{GameID: "rb", Fitness: 0.9, Objective: "balanced", GameKind: "real", At: base.Add(2 * time.Second)})
	s.Record(FitnessSample{GameID: "sh", Fitness: 0.99, Objective: "endurance", GameKind: "shadow", At: base.Add(3 * time.Second)})

	got := s.RecentReal("endurance", 10)
	if len(got) != 2 {
		t.Fatalf("RecentReal(endurance) returned %d samples, want 2 (real endurance only)", len(got))
	}
	// Newest-first: r2 then r1.
	if got[0].GameID != "r2" || got[1].GameID != "r1" {
		t.Errorf("RecentReal order = [%s,%s], want [r2,r1] (newest-first)", got[0].GameID, got[1].GameID)
	}
}

// TestFitnessStore_RecentRealRespectsN: RecentReal caps the result at n.
func TestFitnessStore_RecentRealRespectsN(t *testing.T) {
	s := NewFitnessStore(0)
	base := time.Now()
	for i := 0; i < 5; i++ {
		s.Record(FitnessSample{
			GameID: "r" + string(rune('0'+i)), Fitness: 0.5, Objective: "endurance",
			GameKind: "real", At: base.Add(time.Duration(i) * time.Second),
		})
	}
	if got := s.RecentReal("endurance", 3); len(got) != 3 {
		t.Fatalf("RecentReal(n=3) returned %d, want 3", len(got))
	}
}

// TestRunGameIDsRoundTrip: NewRun encodes game_ids into Run.ID and RunGameIDs
// decodes them, dropping empty fragments.
func TestRunGameIDsRoundTrip(t *testing.T) {
	ids := []string{"game_aaa", "game_bbb", "game_ccc"}
	run := NewRun(ids)
	if run.Games != 3 {
		t.Errorf("run.Games = %d, want 3", run.Games)
	}
	got := RunGameIDs(run)
	if len(got) != 3 || got[0] != "game_aaa" || got[2] != "game_ccc" {
		t.Errorf("RunGameIDs = %v, want %v", got, ids)
	}
	// Empty run → no ids.
	if got := RunGameIDs(Run{}); got != nil {
		t.Errorf("RunGameIDs(empty) = %v, want nil", got)
	}
	// Stray separators are dropped.
	if got := RunGameIDs(Run{ID: "game_x,,game_y,"}); len(got) != 2 {
		t.Errorf("RunGameIDs with stray seps = %v, want 2 ids", got)
	}
}
