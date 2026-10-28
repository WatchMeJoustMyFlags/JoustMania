package main

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"go.uber.org/goleak"
)

// TestRun_ShutdownJoinsShadowGame (#2318): the env-gated shadow game (#778) runs in a
// goroutine that run() tracks, so run() returns, and the deferred tracer / metrics
// shutdown in the process-level run() follows, only after the shadow game has ended
// (its cleanup RPCs and spans included). It holds with and without a background group
// handed in by the caller.
func TestRun_ShutdownJoinsShadowGame(t *testing.T) {
	for _, tc := range []struct {
		name       string
		background *backgroundGroup
	}{
		{name: "with background group", background: &backgroundGroup{}},
		{name: "without background group", background: nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			defer goleak.VerifyNone(t, leakOptions()...)

			c := newShutdownComponents(t)
			c.background = tc.background

			started := make(chan struct{})
			var finished atomic.Bool
			// A shadow-game analogue: bounded by the root ctx, but busy for a moment
			// after the cancellation (the runner's cleanup RPCs and span end).
			c.shadow = func(ctx context.Context) {
				close(started)
				<-ctx.Done()
				time.Sleep(100 * time.Millisecond)
				finished.Store(true)
			}

			ctx, cancel := context.WithCancel(context.Background())
			runDone := make(chan error, 1)
			go func() { runDone <- c.run(ctx) }()
			waitDial(t, c.listenAddr)
			select {
			case <-started:
			case <-time.After(5 * time.Second):
				t.Fatal("shadow game never started")
			}
			cancel()

			select {
			case err := <-runDone:
				if err != nil {
					t.Fatalf("run() returned error: %v", err)
				}
			case <-time.After(10 * time.Second):
				t.Fatal("run() did not return within 10s of context cancellation")
			}
			if !finished.Load() {
				t.Fatal("run() returned before the shadow game goroutine ended")
			}
		})
	}
}
