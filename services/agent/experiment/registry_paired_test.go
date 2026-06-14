package experiment

import (
	"context"
	"fmt"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// registry_paired_test.go covers the registry-side correctness items carried over
// from the #1026 review into #1004: (1) a cap-1 mid-pair termination leaves no
// dangling PairDiff fold and the verdict stays inconclusive; (2) bindGame refuses to
// bind into a terminal experiment (the teardown-vs-bind race widened by CRN's
// two-arm reservation).

// TestCap1MidPairTerminationNoDanglingFold: under effective cap 1 the two arms of a
// pair span two ticks. Spawn + conclude ONLY the experimental arm (pair 0's first
// half), then abort the experiment before the control arm ever spawns. The parked
// half must NOT fold into PairDiff (no fabricated half-pair), the experiment tears
// down cleanly, and the verdict never went conclusive on a half-pair.
func TestCap1MidPairTerminationNoDanglingFold(t *testing.T) {
	spawner := &crnSpawner{}
	targeting := &fakeTargeting{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       2,
		EffectiveConcurrency: 1, // single-game coordinator: one arm per tick.
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000}, // never conclude on its own.
		Targeting:            targeting,
	})
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// Tick 1: reserve+spawn ONE arm of pair 0 (the experimental arm).
	if n := r.AllocateAndSpawn(ctx); n != 1 {
		t.Fatalf("cap-1 tick spawned %d, want 1 (half a pair)", n)
	}
	calls := spawner.snapshot()
	if len(calls) != 1 {
		t.Fatalf("spawner calls = %d, want 1", len(calls))
	}
	firstArm := calls[0].arm
	firstGame := calls[0].gameID

	// Conclude that single game: its half parks in the journal's PendingPairs (a
	// pair cannot complete from one arm). No PairDiff yet.
	if _, err := r.ConcludeGame(ctx, firstGame, 0.80, 1.0); err != nil {
		t.Fatalf("ConcludeGame: %v", err)
	}
	s := r.entries[id].journal.Summary()
	if s.PairDiff != nil {
		t.Fatalf("PairDiff must be nil after only one arm concluded, got %+v", s.PairDiff)
	}
	if len(s.PendingPairs) != 1 {
		t.Fatalf("expected one parked half, PendingPairs=%+v", s.PendingPairs)
	}
	if got := s.PendingPairs[0].Arm; got != firstArm {
		t.Fatalf("parked half arm = %q, want %q", got, firstArm)
	}

	// Abort the experiment before the control arm ever spawns (cap-1 mid-pair
	// termination). The dangling half must NEVER be folded as a half-pair.
	if err := r.Abort(ctx, id, "mid-pair termination test"); err != nil {
		t.Fatalf("Abort: %v", err)
	}

	st, _ := r.Status(id)
	if st != StatusAborted {
		t.Fatalf("status = %q, want aborted", st)
	}
	// Clean teardown: targeting stripped, in-flight cleared.
	if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
		t.Fatalf("teardown should strip targeting once for %s, got %v", id, got)
	}
	if r.InFlight(id) != 0 {
		t.Fatalf("in-flight after teardown = %d, want 0", r.InFlight(id))
	}

	// The verdict, evaluated on the final summary, must stay inconclusive: PairDiff is
	// still nil (no completed pair), so the real paired verdict falls back to the
	// two-arm path, which is under-powered (one arm has 1 sample, the other 0).
	final := r.entries[id].journal.Summary()
	if final.PairDiff != nil {
		t.Fatalf("PairDiff must remain nil after mid-pair teardown, got %+v", final.PairDiff)
	}
	// The parked half is NOT actively drained at teardown — it lingers in this
	// (now terminal, never-reloaded) experiment's own summary. Safety comes from
	// PairDiff staying nil above (the verdict reads only completed pairs), not from
	// draining the half. This pins the documented behavior in PendingHalf's doc.
	if len(final.PendingPairs) != 1 {
		t.Fatalf("parked half should linger after teardown (structurally unfoldable, not drained), PendingPairs=%+v", final.PendingPairs)
	}
	realVerdict := NewVerdictFromEnv()
	v, ok := realVerdict.Evaluate(final)
	if ok && v.Significant {
		t.Fatalf("verdict on a dangling half must not be significant, got %+v", v)
	}
}

// abortingSpawner aborts its experiment from INSIDE Spawn, on the configured 1-based
// call, before returning the game_id — so by the time spawnPair calls bindGame the
// experiment is already terminal. This drives the teardown-vs-bind race
// deterministically.
type abortingSpawner struct {
	mu       sync.Mutex
	r        *Registry
	abortOn  int // 1-based Spawn call on which to abort the experiment
	attempts int
	calls    []spawnCall
	seq      int
}

func (s *abortingSpawner) Spawn(ctx context.Context, experimentID, arm string, seed uint64) (string, error) {
	s.mu.Lock()
	s.attempts++
	n := s.attempts
	s.seq++
	gameID := fmt.Sprintf("game_%012d", s.seq)
	s.calls = append(s.calls, spawnCall{experimentID, arm, gameID, seed})
	s.mu.Unlock()

	if n == s.abortOn {
		// Tear the experiment down WHILE this spawn is in flight: bindGame should then
		// refuse to bind the returned game_id into the now-terminal experiment.
		_ = s.r.Abort(ctx, experimentID, "abort during spawn (terminal-guard test)")
	}
	return gameID, nil
}

func (s *abortingSpawner) snapshot() []spawnCall {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]spawnCall(nil), s.calls...)
}

// TestBindGameRefusesTerminalExperiment: a spawn in flight during teardown must not
// resurrect the experiment's in-flight set. bindGame detects the terminal status,
// records a non-counting release, and binds nothing (#1004 carried-over item 3).
func TestBindGameRefusesTerminalExperiment(t *testing.T) {
	spawner := &abortingSpawner{abortOn: 1}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames:       2,
		EffectiveConcurrency: 1,
		Spawner:              spawner,
		Verdict:              fakeVerdict{concludeAtN: 1000},
		Targeting:            &fakeTargeting{},
	})
	spawner.r = r
	ctx := context.Background()

	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}

	// AllocateAndSpawn reserves an arm, calls Spawn (which aborts the experiment),
	// then bindGame must REFUSE to bind into the terminal experiment.
	bound := r.AllocateAndSpawn(ctx)
	if bound != 0 {
		t.Fatalf("bound %d games, want 0 (bind refused into terminal experiment)", bound)
	}
	if len(spawner.snapshot()) != 1 {
		t.Fatalf("spawner should have been called once, got %d", len(spawner.snapshot()))
	}

	st, _ := r.Status(id)
	if st != StatusAborted {
		t.Fatalf("status = %q, want aborted", st)
	}
	// The experiment must hold NO in-flight games — the refused bind did not
	// resurrect a slot.
	if got := r.InFlight(id); got != 0 {
		t.Fatalf("in-flight = %d, want 0 (no slot leaked into terminal experiment)", got)
	}

	// A non-counting game_released event was recorded for the audit trail; the game
	// was never folded into an arm.
	view := r.entries[id].journal.CompactView()
	released := 0
	concluded := 0
	for _, e := range view.RecentTail {
		switch e.Kind {
		case journal.KindGameReleased:
			released++
		case journal.KindGameConcluded:
			concluded++
		}
	}
	if released == 0 {
		t.Fatalf("expected a game_released event for the refused bind, found none")
	}
	if concluded != 0 {
		t.Fatalf("no game should have concluded, found %d", concluded)
	}
}
