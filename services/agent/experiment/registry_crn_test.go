package experiment

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// pairSeed is a PURE function of (experimentID, pairIndex): same inputs => same
// seed across calls/process restarts (replay-stable), never 0, and different
// inputs => (overwhelmingly) different seeds. This is the #1003 CRN seed contract.
func TestPairSeedIsDeterministicAndNonZero(t *testing.T) {
	if a, b := pairSeed("exp_abc", 0), pairSeed("exp_abc", 0); a != b {
		t.Fatalf("pairSeed not deterministic: %d != %d", a, b)
	}
	if pairSeed("exp_abc", 0) == pairSeed("exp_abc", 1) {
		t.Fatal("consecutive pair indices produced the same seed")
	}
	if pairSeed("exp_abc", 0) == pairSeed("exp_def", 0) {
		t.Fatal("different experiments produced the same seed at pair 0")
	}
	for i := 0; i < 64; i++ {
		if pairSeed("exp_zero_check", i) == 0 {
			t.Fatalf("pairSeed returned 0 (the entropy sentinel) at index %d", i)
		}
	}
}

// crnSpawner records (arm, seed) per call and can be told to fail on the Nth call.
type crnSpawner struct {
	mu      sync.Mutex
	calls   []spawnCall
	seq     int
	failOn  int // 1-based call number to fail on; 0 => never fail
	attempt int
}

func (s *crnSpawner) Spawn(_ context.Context, experimentID, arm string, seed uint64) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.attempt++
	if s.failOn != 0 && s.attempt == s.failOn {
		return "", fmt.Errorf("crn spawner: induced failure on call %d", s.attempt)
	}
	s.seq++
	gameID := fmt.Sprintf("game_%012d", s.seq)
	s.calls = append(s.calls, spawnCall{experimentID, arm, gameID, seed})
	return gameID, nil
}

func (s *crnSpawner) snapshot() []spawnCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]spawnCall(nil), s.calls...)
}

// With room for both arms (effective cap 2), one AllocateAndSpawn reserves and
// spawns a WHOLE pair: both arms carry the SAME seed (CRN), and that seed is the
// pure pairSeed(id, 0). A second pair (after concluding the first) uses pairIndex 1.
func TestPairedArmsShareSeed(t *testing.T) {
	spawner := &crnSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       2,
		EffectiveConcurrency: 2,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if n := r.AllocateAndSpawn(ctx); n != 2 {
		t.Fatalf("spawned %d, want 2 (a full pair)", n)
	}
	calls := spawner.snapshot()
	if len(calls) != 2 {
		t.Fatalf("spawner calls = %d, want 2", len(calls))
	}
	// Both arms of the pair share ONE seed = pairSeed(id, 0).
	want := pairSeed(id, 0)
	for _, c := range calls {
		if c.seed != want {
			t.Fatalf("arm %s seed = %d, want shared pair-0 seed %d", c.arm, c.seed, want)
		}
	}
	if calls[0].arm == calls[1].arm {
		t.Fatalf("both arms = %q, want one experimental + one control", calls[0].arm)
	}

	// game_assigned events carry pair_index 0 for both arms.
	view := r.entries[id].journal.CompactView()
	assignedPairs := 0
	for _, e := range view.RecentTail {
		if e.Kind == journal.KindGameAssigned {
			if e.PairIndex == nil {
				t.Fatalf("game_assigned for %s missing pair_index", e.GameID)
			}
			if *e.PairIndex != 0 {
				t.Fatalf("pair_index = %d, want 0", *e.PairIndex)
			}
			assignedPairs++
		}
	}
	if assignedPairs != 2 {
		t.Fatalf("game_assigned events = %d, want 2", assignedPairs)
	}
}

// Across pairs the seed advances: conclude pair 0, then the next pair's arms share
// a DIFFERENT shared seed = pairSeed(id, 1), still stamped pair_index 1.
func TestSeedAdvancesAcrossPairs(t *testing.T) {
	spawner := &crnSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       2,
		EffectiveConcurrency: 2,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000}, // never conclude the experiment
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Pair 0.
	r.AllocateAndSpawn(ctx)
	first := spawner.snapshot()
	// Conclude both arms of pair 0 to free capacity.
	for _, c := range first {
		if _, err := r.ConcludeGame(ctx, c.gameID, 0.5, 30); err != nil {
			t.Fatalf("ConcludeGame: %v", err)
		}
	}
	// Pair 1.
	r.AllocateAndSpawn(ctx)
	all := spawner.snapshot()
	if len(all) != 4 {
		t.Fatalf("total spawns = %d, want 4 (two pairs)", len(all))
	}
	seed0, seed1 := pairSeed(id, 0), pairSeed(id, 1)
	if first[0].seed != seed0 || first[1].seed != seed0 {
		t.Fatalf("pair 0 seeds = (%d,%d), want both %d", first[0].seed, first[1].seed, seed0)
	}
	if all[2].seed != seed1 || all[3].seed != seed1 {
		t.Fatalf("pair 1 seeds = (%d,%d), want both %d", all[2].seed, all[3].seed, seed1)
	}
	if seed0 == seed1 {
		t.Fatal("pair 0 and pair 1 share a seed; the pair index did not advance")
	}
}

// ATOMIC PAIR ROLLBACK (#1003): when both arms are reserved in one tick (effective
// cap 2) and the SECOND arm's spawn fails, the first arm (already a live game) is
// torn down too — a half-pair must never survive. In-flight returns to 0.
func TestAtomicPairRollbackOnSecondArmFailure(t *testing.T) {
	spawner := &crnSpawner{failOn: 2} // first arm succeeds, second arm fails
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       2,
		EffectiveConcurrency: 2,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	if n := r.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("spawned %d, want 0 (the whole pair rolled back)", n)
	}
	if tot := r.TotalInFlight(); tot != 0 {
		t.Fatalf("in-flight %d after half-pair failure, want 0 (no half-pair survives)", tot)
	}
	if got := r.InFlight(id); got != 0 {
		t.Fatalf("experiment in-flight %d, want 0", got)
	}
}

// Under the single-game coordinator (effective cap 1) a pair cannot be reserved
// atomically; it SPANS TWO TICKS. The two arms must still share the SAME seed
// (pairSeed(id, 0)) and the same pair_index, proving the seed is keyed to the pair
// (armCursor/nArms), not to the tick.
func TestPairSpansTwoTicksUnderEffectiveCapOne(t *testing.T) {
	spawner := &crnSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       4,
		EffectiveConcurrency: 1, // single-game coordinator
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Tick 1: one arm spawns (effective cap 1).
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("tick 1 spawned %d, want 1 (single-game cap)", n)
	}
	first := spawner.snapshot()
	// Conclude it to free the slot.
	if _, err := r.ConcludeGame(ctx, first[0].gameID, 0.5, 30); err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	// Tick 2: the seed-matched sibling spawns.
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("tick 2 spawned %d, want 1", n)
	}
	all := spawner.snapshot()
	if len(all) != 2 {
		t.Fatalf("total spawns = %d, want 2", len(all))
	}
	want := pairSeed(id, 0)
	if all[0].seed != want || all[1].seed != want {
		t.Fatalf("seeds across ticks = (%d,%d), want both pair-0 seed %d", all[0].seed, all[1].seed, want)
	}
	if all[0].arm == all[1].arm {
		t.Fatalf("both arms = %q across ticks, want experimental + control", all[0].arm)
	}
}
