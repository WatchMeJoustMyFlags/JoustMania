package main

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

// TestEvictionLoop_HotReloadsInterval: lifecycle.evict_interval_seconds is read
// live (#927): after a tick the eviction loop re-reads the source and Resets its
// ticker, so a changed cadence applies without a restart.
// UC: UC-041 A2
func TestEvictionLoop_HotReloadsInterval(t *testing.T) {
	t.Run("UC-041/A2 a changed evict interval applies at the next tick without a restart", func(t *testing.T) {
		c := newShutdownComponents(t)
		var live atomic.Int64
		live.Store(int64(5 * time.Millisecond))
		var reads atomic.Int64
		c.evictIntervalSrc = func() time.Duration {
			reads.Add(1)
			return time.Duration(live.Load())
		}

		ctx, cancel := context.WithCancel(context.Background())
		done := c.startEvictionLoop(ctx)
		defer func() {
			cancel()
			<-done
		}()

		// The fast cadence ticks (one read per tick plus the initial one).
		deadline := time.Now().Add(5 * time.Second)
		for reads.Load() < 4 {
			if time.Now().After(deadline) {
				t.Fatalf("eviction loop ticked %d times at 5ms in 5s", reads.Load())
			}
			time.Sleep(time.Millisecond)
		}

		// Slow the cadence down: the next tick picks it up and Resets the ticker.
		live.Store(int64(time.Hour))
		deadline = time.Now().Add(5 * time.Second)
		before := reads.Load()
		for reads.Load() == before {
			if time.Now().After(deadline) {
				t.Fatal("no tick after the interval change")
			}
			time.Sleep(time.Millisecond)
		}
		settled := reads.Load()
		time.Sleep(200 * time.Millisecond) // ~40 ticks at the old 5ms cadence
		if extra := reads.Load() - settled; extra > 1 {
			t.Fatalf("eviction loop ticked %d more times after the interval moved to 1h, want the new cadence applied", extra)
		}
	})
}
