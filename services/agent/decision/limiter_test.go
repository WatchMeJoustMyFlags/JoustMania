package decision

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// These tests exercise the unified weighted rate limiter (ratelimit.go), which
// merges #726's windowLimiter and #728's rateLimiter. They preserve #726's
// limiter coverage (weighted budget, window expiry, concurrency under -race)
// against the surviving rateLimiter.allow API. The intervention-cost weight
// table is covered by TestInterventionCostWeightTable in layers_test.go.

func TestRateLimiter_WeightedBudget(t *testing.T) {
	now := time.Unix(1000, 0)

	t.Run("two medium fit, third denied", func(t *testing.T) {
		var l rateLimiter
		if !l.allow(now, 1, 2) || !l.allow(now, 1, 2) {
			t.Fatal("two medium-cost decisions must fit budget 2")
		}
		if l.allow(now, 1, 2) {
			t.Fatal("third medium-cost decision must be denied")
		}
	})

	t.Run("four soft fit", func(t *testing.T) {
		var l rateLimiter
		for i := 0; i < 4; i++ {
			if !l.allow(now, 0.5, 2) {
				t.Fatalf("soft decision %d must fit budget 2", i+1)
			}
		}
		if l.allow(now, 0.5, 2) {
			t.Fatal("fifth soft decision must be denied")
		}
	})

	t.Run("one hard exhausts the budget", func(t *testing.T) {
		var l rateLimiter
		if !l.allow(now, 2, 2) {
			t.Fatal("hard decision must fit a fresh budget")
		}
		if l.allow(now, 0.5, 2) {
			t.Fatal("nothing fits after a hard decision")
		}
	})

	t.Run("budget <= 0 admits nothing", func(t *testing.T) {
		var l rateLimiter
		if l.allow(now, 0.5, 0) {
			t.Fatal("a zero budget must be fully closed")
		}
	})
}

func TestRateLimiter_WindowExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	var l rateLimiter
	if !l.allow(now, 2, 2) {
		t.Fatal("hard decision must fit")
	}
	if l.allow(now.Add(30*time.Second), 1, 2) {
		t.Fatal("budget must still be exhausted inside the window")
	}
	if !l.allow(now.Add(61*time.Second), 2, 2) {
		t.Fatal("budget must recover after the window expires")
	}
}

func TestRateLimiter_Concurrent(t *testing.T) {
	now := time.Unix(1000, 0)
	var l rateLimiter
	var admitted atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if l.allow(now, 0.5, 2) {
				admitted.Add(1)
			}
		}()
	}
	wg.Wait()
	if got := admitted.Load(); got != 4 {
		t.Fatalf("admitted = %d, want exactly 4 (0.5 cost x budget 2) under concurrency", got)
	}
}

func TestCostOf_MatchesInterventionCost(t *testing.T) {
	// costOf is the engine's alias for interventionCost (used for tie-breaking).
	for _, intervention := range []string{
		InterventionPlayAudioCue, InterventionAdjustMusicTempo,
		InterventionEndGame, "unknown_future_intervention",
	} {
		if got, want := costOf(intervention), interventionCost(intervention); got != want {
			t.Errorf("costOf(%s) = %v, want %v (interventionCost)", intervention, got, want)
		}
	}
}
