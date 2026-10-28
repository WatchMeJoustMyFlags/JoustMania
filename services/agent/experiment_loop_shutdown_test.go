package main

import (
	"context"
	"sync/atomic"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/promote"
)

// experiment_loop_shutdown_test.go (#2323): a slot refill fired off the tick goroutine
// (a terminal outcome, a stale release, a conclude, a grace backstop) runs with run()'s
// context, so once shutdown has cancelled it the refill reads no gate and spawns no new
// shadow game. Before #2323 these paths used context.Background() and refilled anyway.

// newRefillLoop returns a loop with one live experiment and one game in flight, and a
// counter of the refill's gate reads (allocateIfEnabled reads the kill switch first).
func newRefillLoop(t *testing.T) (*experimentLoop, *recordingSpawner, string, string, *atomic.Int32) {
	t.Helper()
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
	var gateReads atomic.Int32
	loop.enabledOverride = func(context.Context) bool {
		gateReads.Add(1)
		return true
	}
	ctx := context.Background()
	id, err := loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "balanced", TargetNPerArm: 3,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected a spawn")
	}
	gameID := spawner.drain()[0].gameID
	return loop, spawner, id, gameID, &gateReads
}

func TestExperimentLoop_RefillAfterShutdownSpawnsNothing(t *testing.T) {
	paths := map[string]func(loop *experimentLoop, expID, gameID string){
		"terminal outcome": func(loop *experimentLoop, _, gameID string) {
			loop.onGameTerminal(gameID, gamerunner.OutcomeError)
		},
		"stale release": func(loop *experimentLoop, expID, gameID string) {
			var gc gamecontext.GameContext
			gc.SessionID, gc.ExperimentID = gameID, expID
			loop.releaseStaleGame(gc, experiment.ReleaseReasonStale)
		},
	}
	for name, release := range paths {
		t.Run(name+": cancelled run context refills nothing", func(t *testing.T) {
			loop, spawner, expID, gameID, gateReads := newRefillLoop(t)
			ctx, cancel := context.WithCancel(context.Background())
			loop.runCtx.Store(&ctx)
			cancel() // the SIGTERM analogue

			release(loop, expID, gameID)

			if gameStillBound(loop, expID, gameID) {
				t.Fatal("the game's slot was not released")
			}
			if n := gateReads.Load(); n != 0 {
				t.Fatalf("refill read the gates %d time(s) after shutdown, want 0", n)
			}
			if got := spawner.drain(); len(got) != 0 {
				t.Fatalf("refill spawned %d shadow game(s) after shutdown, want 0", len(got))
			}
		})
		t.Run(name+": live run context refills", func(t *testing.T) {
			loop, _, expID, gameID, gateReads := newRefillLoop(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			loop.runCtx.Store(&ctx)

			release(loop, expID, gameID)

			if gateReads.Load() == 0 {
				t.Fatal("refill did not run while the agent is live")
			}
		})
	}
}
