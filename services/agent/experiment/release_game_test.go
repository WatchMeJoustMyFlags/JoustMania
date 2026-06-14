package experiment

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// TestReleaseGameFreesSlotWithoutFoldingSample covers the #1014 backstop: a game
// that ended WITHOUT a usable fitness sample (game_error / force-end) is released
// via ReleaseGame, which must free the in-flight slot WITHOUT folding a fitness
// sample into the arm (so a transient failure cannot pollute the cohort).
func TestReleaseGameFreesSlotWithoutFoldingSample(t *testing.T) {
	spawner := &fakeSpawner{}
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
	if n := r.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected at least one spawn")
	}
	if r.TotalInFlight() == 0 {
		t.Fatal("expected in-flight games after spawn")
	}

	// The spawner recorded the bound game ids; release the first one as a failure.
	gameID := spawner.calls[0].gameID
	beforeInFlight := r.TotalInFlight()
	if !r.ReleaseGame(gameID, "game ended without usable fitness sample: error") {
		t.Fatal("ReleaseGame returned false for a bound game")
	}
	if got := r.TotalInFlight(); got != beforeInFlight-1 {
		t.Fatalf("in-flight = %d after release, want %d", got, beforeInFlight-1)
	}

	// No fitness sample was folded: every arm's count stays 0.
	cv, _ := r.CompactView(id)
	for arm, a := range cv.Arms {
		if a.Count != 0 {
			t.Fatalf("arm %q count = %d after release, want 0 (release must not fold a sample)", arm, a.Count)
		}
	}

	// A game_released audit event is recorded; no game_concluded.
	var released, concluded int
	for _, ev := range cv.RecentTail {
		switch ev.Kind {
		case journal.KindGameReleased:
			released++
		case journal.KindGameConcluded:
			concluded++
		}
	}
	if released != 1 {
		t.Fatalf("game_released events = %d, want 1", released)
	}
	if concluded != 0 {
		t.Fatalf("game_concluded events = %d, want 0 (release is non-counting)", concluded)
	}
}

// TestReleaseGameIsIdempotent confirms ReleaseGame is safe to call alongside the
// telemetry-driven ConcludeGame path: a second release (or a release after
// conclude) for an unknown/already-handled game_id is a no-op returning false.
func TestReleaseGameIsIdempotent(t *testing.T) {
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 1, EffectiveConcurrency: 1,
		Spawner: spawner, Verdict: fakeVerdict{concludeAtN: 1000},
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	_ = r.Start(ctx, id)
	r.AllocateAndSpawn(ctx)
	gameID := spawner.calls[0].gameID

	if !r.ReleaseGame(gameID, "first") {
		t.Fatal("first ReleaseGame should free the slot")
	}
	if r.ReleaseGame(gameID, "second") {
		t.Fatal("second ReleaseGame for the same game must be a no-op (false)")
	}
	if r.ReleaseGame("game_never_existed", "x") {
		t.Fatal("ReleaseGame for an unknown game must return false")
	}
}

// TestErroredGameDoesNotDeadlockAtConcurrencyOne is the #1014 regression: at
// EffectiveConcurrency=1 the coordinator runs one game at a time, so a single
// errored game that leaks its in-flight slot deadlocks the loop forever. Releasing
// the errored game must free the only slot so the NEXT AllocateAndSpawn spawns
// again (the loop keeps progressing).
func TestErroredGameDoesNotDeadlockAtConcurrencyOne(t *testing.T) {
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       4, // capacity bookkeeping allows more...
		EffectiveConcurrency: 1, // ...but only ONE in-flight at a time (#998).
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000}, // never concludes on its own.
	})
	ctx := context.Background()
	id, _ := r.Declare(sampleIntent())
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// First tick: exactly one game spawns (the effective cap is 1).
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("first allocate spawned %d, want 1 (effective cap)", n)
	}
	if r.TotalInFlight() != 1 {
		t.Fatalf("in-flight = %d, want 1", r.TotalInFlight())
	}

	// While that game is in-flight the loop is FULL: another tick spawns nothing.
	if n := r.AllocateAndSpawn(ctx); n != 0 {
		t.Fatalf("allocate while full spawned %d, want 0", n)
	}

	// The game errors. WITHOUT the #1014 fix the slot leaks and the loop is stuck
	// at in_flight=1 forever. ReleaseGame frees the slot.
	gameID := spawner.calls[0].gameID
	if !r.ReleaseGame(gameID, "game ended without usable fitness sample: error") {
		t.Fatal("ReleaseGame did not free the errored game's slot")
	}
	if r.TotalInFlight() != 0 {
		t.Fatalf("in-flight = %d after release, want 0 (slot freed, no deadlock)", r.TotalInFlight())
	}

	// The loop continues: the next tick spawns the next game (no deadlock).
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("allocate after release spawned %d, want 1 (loop progresses)", n)
	}
	if spawner.count() != 2 {
		t.Fatalf("total spawns = %d, want 2 (the errored slot was reused)", spawner.count())
	}
}
