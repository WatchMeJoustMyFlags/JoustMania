package main

import (
	"context"
	"errors"
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
		"conclude": func(loop *experimentLoop, expID, gameID string) {
			var gc gamecontext.GameContext
			gc.SessionID, gc.ExperimentID = gameID, expID
			loop.concludeExperimentGame(gc)
		},
		"parked end": func(loop *experimentLoop, _, gameID string) {
			loop.graceMu.Lock()
			loop.parked = map[string]gamecontext.GameContext{gameID: {}}
			loop.graceMu.Unlock()
			loop.fireParkedEnd(gameID)
		},
		"completed grace": func(loop *experimentLoop, _, gameID string) {
			loop.fireCompletedGrace(gameID)
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

// UC-043 A20 (#2332): a game-end proposer that races the teardown starts no
// experiment once run() has begun its shutdown (it AbortAll's and returns), and a
// conclusion that lands during shutdown does not write the real default.
// UC: UC-043 A20
func TestUC043_A20_LoopShutdown(t *testing.T) {
	t.Run("UC-043/A20 a proposal after the shutdown AbortAll starts no experiment", func(t *testing.T) {
		resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
		loop := buildLoopForTest(t, &recordingSpawner{}, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan struct{})
		go func() {
			defer close(done)
			loop.run(ctx)
		}()
		cancel() // the SIGTERM analogue
		<-done   // run() has run its AbortAll

		err := loop.proposalSink().Review(context.Background(), experiment.Proposal{
			FlagKey: "death_grace_period_seconds", ExperimentalValue: 0.5, Rationale: "late proposer",
		})

		if !errors.Is(err, experiment.ErrShuttingDown) {
			t.Fatalf("Review after shutdown = %v, want ErrShuttingDown", err)
		}
		if n := loop.registry.LiveCount(); n != 0 {
			t.Fatalf("live experiments after shutdown = %d, want 0", n)
		}
	})

	t.Run("UC-043/A20 an autonomous promote verdict during shutdown writes no real default", func(t *testing.T) {
		spawner := &recordingSpawner{}
		realDef := &recordingRealDefault{}
		resolve := func(context.Context) promote.Config {
			return promote.Config{Mode: promote.ModeAutonomous, Target: promote.TargetLocal, Enabled: true}
		}
		loop := buildLoopForTest(t, spawner, resolve, realDef, &recordingGitHub{}, armFitness())
		id, err := loop.registry.Declare(experiment.Intent{
			FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
		})
		if err != nil {
			t.Fatalf("declare: %v", err)
		}
		if err := loop.registry.Start(context.Background(), id); err != nil {
			t.Fatalf("start: %v", err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		loop.registry.SetShutdownContext(ctx)
		cancel() // shutdown began; the loop's AbortAll has not run yet

		concludeCohort(t, loop, spawner, 4)

		if got := realDef.count(); got != 0 {
			t.Fatalf("real-default writes during shutdown = %d, want 0", got)
		}
		if st, _ := loop.registry.Status(id); st != experiment.StatusDiscarded {
			t.Fatalf("experiment status = %q, want discarded", st)
		}
		if experiment.PromotionReasonShutdown != promote.ReasonShutdown {
			t.Fatalf("shutdown reasons differ: %q vs %q", experiment.PromotionReasonShutdown, promote.ReasonShutdown)
		}
	})
}
