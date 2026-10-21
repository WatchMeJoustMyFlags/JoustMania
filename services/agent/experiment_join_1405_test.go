package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/patterngame"
	"github.com/joustmania/agent/promote"
)

// experiment_join_1405_test.go covers the end join of #1405 (UC-046 BR-016): a
// bound experiment game folds its fitness sample only once BOTH its telemetry end
// and the runner's terminal outcome are in, and only for a sample outcome.

// joinRig is one experiment loop with a fake clock, a counting fitness func (the
// fold counter: concludeExperimentGame scores exactly once per fold), a fitness
// store and a span recorder.
type joinRig struct {
	loop    *experimentLoop
	spawner *recordingSpawner
	fake    *durationAfter
	sr      *tracetest.SpanRecorder
	scored  *atomic.Int64
	id      string
}

// durationAfter is fakeAfter that also records each scheduled window.
type durationAfter struct {
	*fakeAfter
	mu    sync.Mutex
	waits []time.Duration
}

func (d *durationAfter) schedule(w time.Duration, fn func()) func() bool {
	d.mu.Lock()
	d.waits = append(d.waits, w)
	d.mu.Unlock()
	return d.fakeAfter.schedule(w, fn)
}

func (d *durationAfter) lastWait() time.Duration {
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.waits) == 0 {
		return 0
	}
	return d.waits[len(d.waits)-1]
}

func newJoinRig(t *testing.T, grace time.Duration, fitness gameFitnessFunc) *joinRig {
	t.Helper()
	spawner := &recordingSpawner{}
	resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
	scored := &atomic.Int64{}
	counting := func(gc gamecontext.GameContext, objective string) float64 {
		scored.Add(1)
		return fitness(gc, objective)
	}
	loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, counting)
	fake := &durationAfter{fakeAfter: newFakeAfter()}
	loop.afterFunc = fake.schedule
	loop.expDefaults.CompletedGrace = grace
	loop.fitnessStore = experiment.NewFitnessStore(0)
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	loop.tracer = tp.Tracer("test")
	return &joinRig{loop: loop, spawner: spawner, fake: fake, sr: sr, scored: scored}
}

// start declares and starts one experiment and returns its first bound game.
func (r *joinRig) start(t *testing.T, targetN int) spawnRec {
	t.Helper()
	ctx := context.Background()
	id, err := r.loop.registry.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "death_grace_period_seconds",
		ExperimentalValue: 0.5, Objective: "balanced", TargetNPerArm: targetN,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.loop.registry.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	r.id = id
	if n := r.loop.registry.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("expected a spawn")
	}
	recs := r.spawner.drain()
	if len(recs) == 0 {
		t.Fatal("spawner recorded no game")
	}
	return recs[0]
}

func (r *joinRig) folded() int {
	cv, _ := r.loop.registry.CompactView(r.id)
	n := 0
	for _, a := range cv.Arms {
		n += a.Count
	}
	return n
}

func (r *joinRig) fitnessSpans() int {
	return len(expSpan_findByName(r.sr.Ended(), decision.SpanExperimentFitness))
}

// assertJoinStateEmpty checks that no parked end, recorded outcome or timer is left.
func assertJoinStateEmpty(t *testing.T, loop *experimentLoop) {
	t.Helper()
	loop.graceMu.Lock()
	defer loop.graceMu.Unlock()
	if len(loop.parked) != 0 || len(loop.outcomes) != 0 || len(loop.graceTimers) != 0 {
		t.Fatalf("join state left: parked=%d outcomes=%d timers=%d, want all 0",
			len(loop.parked), len(loop.outcomes), len(loop.graceTimers))
	}
}

func releaseNoteOf(loop *experimentLoop, expID, gameID string) string {
	cv, _ := loop.registry.CompactView(expID)
	for _, ev := range cv.RecentTail {
		if ev.GameID == gameID && ev.Kind == journal.KindGameReleased {
			return ev.Note
		}
	}
	return ""
}

func shadowEnd(rec spawnRec) gamecontext.GameContext {
	gc := gcFor(rec.gameID, rec.experimentID, rec.arm)
	gc.EndReason = gamecontext.EndReasonZero
	return gc
}

// UC: UC-043 A2 A12
// UC: UC-046 A8 BR-015 BR-016
func TestExperimentLoop_EndJoin_TelemetryFirst(t *testing.T) {
	for _, tc := range []struct {
		name    string
		outcome gamerunner.Outcome
		note    string
	}{
		{"UC-043/A2 telemetry end before preemption folds no sample", gamerunner.OutcomePreempted, experiment.ReleaseReasonPreempted},
		{"UC-043/A12 telemetry end before error folds no sample", gamerunner.OutcomeError, "game ended without usable fitness sample: error"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rig := newJoinRig(t, 60*time.Second, armFitness())
			rec := rig.start(t, 3)

			rig.loop.onGameEnd(shadowEnd(rec))
			rig.loop.onGameTerminal(rec.gameID, tc.outcome)

			if n := rig.scored.Load(); n != 0 {
				t.Fatalf("game scored %d times, want 0", n)
			}
			if n := rig.folded(); n != 0 {
				t.Fatalf("arm counts = %d, want 0 (no sample)", n)
			}
			if gameStillBound(rig.loop, rig.id, rec.gameID) {
				t.Fatal("game kept its binding")
			}
			if note := releaseNoteOf(rig.loop, rig.id, rec.gameID); note != tc.note {
				t.Fatalf("release note = %q, want %q", note, tc.note)
			}
			if _, ok := rig.loop.fitnessStore.Get(rec.gameID); ok {
				t.Fatal("a finalized fitness was written for a game without a sample")
			}
			if n := rig.fitnessSpans(); n != 0 {
				t.Fatalf("experiment.fitness spans = %d, want 0", n)
			}
			assertJoinStateEmpty(t, rig.loop)
		})
	}

	t.Run("UC-046/BR-016 natural end with a completed outcome folds once in either order", func(t *testing.T) {
		for _, telemetryFirst := range []bool{true, false} {
			rig := newJoinRig(t, 60*time.Second, armFitness())
			rec := rig.start(t, 3)
			gc := shadowEnd(rec)
			if telemetryFirst {
				rig.loop.onGameEnd(gc)
				if rig.scored.Load() != 0 {
					t.Fatal("a telemetry end folded before the runner outcome")
				}
				rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted)
			} else {
				rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted)
				rig.loop.onGameEnd(gc)
			}
			if n := rig.scored.Load(); n != 1 {
				t.Fatalf("telemetryFirst=%v: scored %d, want 1", telemetryFirst, n)
			}
			if n := rig.folded(); n != 1 {
				t.Fatalf("telemetryFirst=%v: folded %d, want 1", telemetryFirst, n)
			}
			got, ok := rig.loop.fitnessStore.Get(rec.gameID)
			if want := armFitness()(gc, "balanced"); !ok || got.Fitness != want {
				t.Fatalf("telemetryFirst=%v: finalized fitness = %v (ok=%v), want %v", telemetryFirst, got.Fitness, ok, want)
			}
			if note := releaseNoteOf(rig.loop, rig.id, rec.gameID); note != "" {
				t.Fatalf("telemetryFirst=%v: a sample game was released (%q)", telemetryFirst, note)
			}
			assertJoinStateEmpty(t, rig.loop)
		}
	})

	// The coordinator zeroes game_active before the preemption's game_force_ended,
	// and the runner classifies game_ended{winner} then game_force_ended{preempted}
	// as completed (gamerunner TestAwaitTerminal_OutcomeClassification, "natural
	// winner is a sample even when a preemption follows"): one fold, no release.
	t.Run("UC-046/BR-016 natural end racing a preemption folds once and releases nothing", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameEnd(shadowEnd(rec))
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted)
		if n := rig.folded(); n != 1 {
			t.Fatalf("folded %d, want 1", n)
		}
		cv, _ := rig.loop.registry.CompactView(rig.id)
		for _, ev := range cv.RecentTail {
			if ev.Kind == journal.KindGameReleased {
				t.Fatalf("release %q, want none", ev.Note)
			}
		}
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-046 A8 BR-015 BR-016
func TestExperimentLoop_EndJoin_PreemptedPairReruns(t *testing.T) {
	t.Run("UC-046/BR-015 a preempted pair reruns when telemetry wins", func(t *testing.T) {
		arch, errs := patterngame.NewArchetype("only", map[string]any{
			"players": 4.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
			"period_s": 3.0, "phase": 0.0, "burst_h": 0.0, "burst_period_s": 0.0,
			"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
		})
		if len(errs) > 0 {
			t.Fatalf("archetype: %v", errs)
		}
		rig := newJoinRig(t, 60*time.Second, armFitness())
		resolve := func(context.Context) promote.Config { return promote.Config{Mode: promote.ModeIssue} }
		log := testLogger()
		writer := experiment.NewWriter(newGameFileForLoop(t), log)
		targeting := experiment.NewGateTargetingWriter(experiment.NewGate(writer, nil, log), writer)
		base := promote.NewPromoter(&recordingGitHub{}, nil, &recordingRealDefault{}, nil, nil, promote.RepoRef{}, nil, log)
		rig.loop.registry = experiment.NewRegistry(experiment.RegistryConfig{
			Root: t.TempDir(), MaxShadowGames: 2, EffectiveConcurrency: 1, Spawner: rig.spawner,
			Verdict: experiment.NewVerdict(3, 0.5, nil), Targeting: targeting, Log: log,
			Promoter: promote.NewExperimentPromoter(base, resolve),
			Patterns: &countingPinner{pin: patterngame.Pin{Selection: "only", MaxGameSeconds: 300, Archetypes: []patterngame.Archetype{arch}}},
		})
		first := rig.start(t, 50)

		rig.loop.onGameEnd(shadowEnd(first))
		rig.loop.onGameTerminal(first.gameID, gamerunner.OutcomePreempted)

		if n := rig.scored.Load(); n != 0 || rig.folded() != 0 || rig.fitnessSpans() != 0 {
			t.Fatalf("preempted game: scored=%d folded=%d fitness spans=%d, want 0/0/0", n, rig.folded(), rig.fitnessSpans())
		}
		cv, _ := rig.loop.registry.CompactView(rig.id)
		preempted, counting := 0, 0
		for _, ev := range cv.RecentTail {
			switch ev.Note {
			case "pattern_pair_rerun_preempted":
				preempted++
			case "pattern_pair_rerun":
				counting++
			}
		}
		if preempted != 1 || counting != 0 {
			t.Fatalf("journal notes: pattern_pair_rerun_preempted=%d pattern_pair_rerun=%d, want 1/0", preempted, counting)
		}

		// The release refilled the slot with the pair's rerun: same pair index, and
		// its own telemetry-first natural end folds exactly one sample.
		rerun := rig.spawner.drain()
		if len(rerun) != 1 {
			t.Fatalf("rerun spawns = %d, want 1", len(rerun))
		}
		pairOf := func(gameID string) *int {
			cv, _ := rig.loop.registry.CompactView(rig.id)
			for _, ev := range cv.RecentTail {
				if ev.GameID == gameID && ev.Kind == journal.KindGameAssigned {
					return ev.PairIndex
				}
			}
			return nil
		}
		p0, p1 := pairOf(first.gameID), pairOf(rerun[0].gameID)
		if p0 == nil || p1 == nil || *p0 != *p1 {
			t.Fatalf("rerun pair index = %v, want the preempted game's %v", p1, p0)
		}
		rig.loop.onGameEnd(shadowEnd(rerun[0]))
		rig.loop.onGameTerminal(rerun[0].gameID, gamerunner.OutcomeCompleted)
		if n := rig.folded(); n != 1 {
			t.Fatalf("folded %d after the rerun's natural end, want 1", n)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-046 BR-016
func TestExperimentLoop_EndJoin_OutcomeFirst(t *testing.T) {
	t.Run("UC-046/BR-016 preemption first, then telemetry: no fold", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomePreempted)
		rig.loop.onGameEnd(shadowEnd(rec))
		if n := rig.scored.Load(); n != 0 {
			t.Fatalf("scored %d, want 0", n)
		}
		if _, ok := rig.loop.fitnessStore.Get(rec.gameID); ok {
			t.Fatal("finalized fitness written for a preempted game")
		}
		if n := rig.fitnessSpans(); n != 0 {
			t.Fatalf("experiment.fitness spans = %d, want 0", n)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-046/BR-013 timeout first, then telemetry: folds", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeTimeout)
		rig.loop.onGameEnd(shadowEnd(rec))
		if n := rig.folded(); n != 1 {
			t.Fatalf("folded %d, want 1", n)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-046/BR-016 a recorded outcome with no telemetry end expires", func(t *testing.T) {
		rig := newJoinRig(t, 0, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeError)
		if w := rig.fake.lastWait(); w != parkFloor {
			t.Fatalf("outcome expiry window = %s, want %s", w, parkFloor)
		}
		rig.fake.fireAll()
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-046 BR-016
func TestExperimentLoop_EndJoin_ParkBackstop(t *testing.T) {
	t.Run("UC-046/BR-016 a parked end with no outcome is released without a sample", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameEnd(shadowEnd(rec))
		if w := rig.fake.lastWait(); w != 60*time.Second {
			t.Fatalf("park window = %s, want the 60 s grace", w)
		}
		if !gameStillBound(rig.loop, rig.id, rec.gameID) {
			t.Fatal("a parked game lost its binding before the window ran out")
		}
		if fired := rig.fake.fireAll(); fired != 1 {
			t.Fatalf("timers fired = %d, want 1", fired)
		}
		if n := rig.scored.Load(); n != 0 || rig.folded() != 0 {
			t.Fatalf("backstop folded: scored=%d folded=%d, want 0/0", n, rig.folded())
		}
		if note := releaseNoteOf(rig.loop, rig.id, rec.gameID); note != releaseReasonNoOutcome {
			t.Fatalf("release note = %q, want %q", note, releaseReasonNoOutcome)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-046/BR-016 grace off still parks for the floor and keeps a natural sample", func(t *testing.T) {
		rig := newJoinRig(t, 0, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameEnd(shadowEnd(rec))
		if w := rig.fake.lastWait(); w != parkFloor || w <= 0 {
			t.Fatalf("park window with grace off = %s, want the %s floor", w, parkFloor)
		}
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted)
		if n := rig.folded(); n != 1 {
			t.Fatalf("folded %d with grace off, want 1", n)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-046/BR-016 a foreign or unbound end takes the direct path", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rig.start(t, 3)
		rig.loop.onGameEnd(gcFor("game_foreign", rig.id, experiment.ArmControl))
		if n := rig.scored.Load(); n != 1 {
			t.Fatalf("unbound end scored %d, want 1 (today's direct path)", n)
		}
		if rig.fake.pendingCount() != 0 {
			t.Fatal("an unbound end was parked")
		}
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-041 A4
// UC: UC-046 BR-015 BR-016
func TestExperimentLoop_EndJoin_StaleCleansJoinState(t *testing.T) {
	t.Run("UC-046/BR-015 a stale end after a preemption keeps the preemption reason", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomePreempted)
		stale := shadowEnd(rec)
		stale.EndReason = gamecontext.EndReasonStale
		rig.loop.onGameEnd(stale)
		if note := releaseNoteOf(rig.loop, rig.id, rec.gameID); note != experiment.ReleaseReasonPreempted {
			t.Fatalf("release note = %q, want %q", note, experiment.ReleaseReasonPreempted)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-041/A4 a stale end drops a parked end", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.onGameEnd(shadowEnd(rec))
		stale := shadowEnd(rec)
		stale.EndReason = gamecontext.EndReasonStale
		rig.loop.onGameEnd(stale)
		if note := releaseNoteOf(rig.loop, rig.id, rec.gameID); note != experiment.ReleaseReasonStale {
			t.Fatalf("release note = %q, want %q", note, experiment.ReleaseReasonStale)
		}
		if rig.scored.Load() != 0 {
			t.Fatal("stale game scored")
		}
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-046 BR-016
func TestExperimentLoop_EndJoin_Shutdown(t *testing.T) {
	t.Run("UC-046/BR-016 a park firing after shutdown releases and spawns nothing", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		var mu sync.Mutex
		var callbacks []func()
		rig.loop.afterFunc = func(_ time.Duration, fn func()) func() bool {
			mu.Lock()
			callbacks = append(callbacks, fn)
			mu.Unlock()
			return func() bool { return false } // too late to stop
		}
		rec := rig.start(t, 3)
		rig.loop.onGameEnd(shadowEnd(rec))
		rig.loop.stopAllCompletedGrace()
		mu.Lock()
		fns := append([]func(){}, callbacks...)
		mu.Unlock()
		for _, fn := range fns {
			fn()
		}
		if !gameStillBound(rig.loop, rig.id, rec.gameID) {
			t.Fatal("a park fired after shutdown released the game")
		}
		if n := len(rig.spawner.drain()); n != 0 {
			t.Fatalf("spawned %d games after shutdown, want 0", n)
		}
		assertJoinStateEmpty(t, rig.loop)
	})
	t.Run("UC-046/BR-016 a telemetry end after shutdown is neither parked nor folded", func(t *testing.T) {
		rig := newJoinRig(t, 60*time.Second, armFitness())
		rec := rig.start(t, 3)
		rig.loop.stopAllCompletedGrace()
		rig.loop.onGameEnd(shadowEnd(rec))
		if rig.scored.Load() != 0 || rig.fake.pendingCount() != 0 {
			t.Fatalf("after shutdown: scored=%d timers=%d, want 0/0", rig.scored.Load(), rig.fake.pendingCount())
		}
		assertJoinStateEmpty(t, rig.loop)
	})
}

// UC: UC-046 BR-016
func TestExperimentLoop_EndJoin_Race(t *testing.T) {
	t.Run("UC-046/BR-016 concurrent telemetry end and completed outcome fold exactly once", func(t *testing.T) {
		// A constant fitness keeps the verdict inconclusive, so the experiment keeps
		// spawning for every iteration.
		rig := newJoinRig(t, 60*time.Second, func(gamecontext.GameContext, string) float64 { return 0.5 })
		first := rig.start(t, 100000)
		queue := []spawnRec{first}
		const iterations = 200
		for i := 0; i < iterations; i++ {
			queue = append(queue, rig.spawner.drain()...)
			if len(queue) == 0 {
				rig.loop.registry.AllocateAndSpawn(context.Background())
				queue = append(queue, rig.spawner.drain()...)
			}
			if len(queue) == 0 {
				t.Fatalf("iteration %d: no bound game to end", i)
			}
			rec := queue[0]
			queue = queue[1:]
			before := rig.scored.Load()
			var wg sync.WaitGroup
			wg.Add(2)
			go func() { defer wg.Done(); rig.loop.onGameEnd(shadowEnd(rec)) }()
			go func() { defer wg.Done(); rig.loop.onGameTerminal(rec.gameID, gamerunner.OutcomeCompleted) }()
			wg.Wait()
			if got := rig.scored.Load() - before; got != 1 {
				t.Fatalf("iteration %d: folds = %d, want exactly 1", i, got)
			}
		}
		if n := rig.folded(); n != iterations {
			t.Fatalf("arm counts = %d, want %d", n, iterations)
		}
		assertJoinStateEmpty(t, rig.loop)
		if n := rig.fake.pendingCount(); n != 0 {
			t.Fatalf("pending timers = %d, want 0", n)
		}
	})
}
