package decision

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestWindowLimiter_WeightedBudget(t *testing.T) {
	now := time.Unix(1000, 0)
	var l windowLimiter

	t.Run("two medium fit, third denied", func(t *testing.T) {
		l = windowLimiter{}
		if !l.admit(now, 1, 2) || !l.admit(now, 1, 2) {
			t.Fatal("two medium-cost decisions must fit budget 2")
		}
		if l.admit(now, 1, 2) {
			t.Fatal("third medium-cost decision must be denied")
		}
	})

	t.Run("four soft fit", func(t *testing.T) {
		l = windowLimiter{}
		for i := 0; i < 4; i++ {
			if !l.admit(now, 0.5, 2) {
				t.Fatalf("soft decision %d must fit budget 2", i+1)
			}
		}
		if l.admit(now, 0.5, 2) {
			t.Fatal("fifth soft decision must be denied")
		}
	})

	t.Run("one hard exhausts the budget", func(t *testing.T) {
		l = windowLimiter{}
		if !l.admit(now, 2, 2) {
			t.Fatal("hard decision must fit a fresh budget")
		}
		if l.admit(now, 0.5, 2) {
			t.Fatal("nothing fits after a hard decision")
		}
	})
}

func TestWindowLimiter_WindowExpiry(t *testing.T) {
	now := time.Unix(1000, 0)
	var l windowLimiter
	if !l.admit(now, 2, 2) {
		t.Fatal("hard decision must fit")
	}
	if l.admit(now.Add(30*time.Second), 1, 2) {
		t.Fatal("budget must still be exhausted inside the window")
	}
	if !l.admit(now.Add(61*time.Second), 2, 2) {
		t.Fatal("budget must recover after the window expires")
	}
}

func TestWindowLimiter_Concurrent(t *testing.T) {
	now := time.Unix(1000, 0)
	var l windowLimiter
	var admitted atomic.Int32
	var wg sync.WaitGroup
	for i := 0; i < 100; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if l.admit(now, 0.5, 2) {
				admitted.Add(1)
			}
		}()
	}
	wg.Wait()
	if got := admitted.Load(); got != 4 {
		t.Fatalf("admitted = %d, want exactly 4 (0.5 cost x budget 2) under concurrency", got)
	}
}

func TestCostOf(t *testing.T) {
	cases := map[string]float64{
		InterventionPlayAudioCue:            0.5,
		InterventionAdjustMusicTempo:        1,
		InterventionEndGame:                 2,
		"unknown_future_intervention":       2, // fail safe: unknown = hard
		InterventionAdjustPlayerSensitivity: 1,
	}
	for intervention, want := range cases {
		if got := costOf(intervention); got != want {
			t.Errorf("costOf(%s) = %v, want %v", intervention, got, want)
		}
	}
}
