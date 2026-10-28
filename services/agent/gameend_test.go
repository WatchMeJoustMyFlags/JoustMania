package main

import (
	"context"
	"strings"
	"testing"
	"time"

	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/promote"
)

// gameend_test.go covers the game-end chain's handling of the three end signals
// (UC-041 A4, BR-005, #1494): the duration game-end consumers derive, the stale-end
// exclusion from experiments, retro, summary and proposer, and the stale counter.

// endClock is a settable test clock.
type endClock struct{ t time.Time }

func (c *endClock) now() time.Time          { return c.t }
func (c *endClock) advance(d time.Duration) { c.t = c.t.Add(d) }
func newEndClock() *endClock                { return &endClock{t: time.Unix(1_000, 0)} }
func captureEnd(dst *[]gamecontext.GameContext) func(gamecontext.GameContext) {
	return func(c gamecontext.GameContext) { *dst = append(*dst, c) }
}

// UC: UC-041 A4 BR-003
func TestWithEffectiveDuration_LateAndStaleEnds(t *testing.T) {
	t.Run("UC-041/A4 root span arriving 5 s late: duration is start -> span end", func(t *testing.T) {
		clk := newEndClock()
		var ends []gamecontext.GameContext
		s := gamecontext.NewStore(time.Hour, time.Hour, clk.now)
		s.OnGameEnd = captureEnd(&ends)
		s.SetGameActive(true)
		clk.advance(20 * time.Second)
		spanEnd := clk.now()
		clk.advance(5 * time.Second)
		s.EndGameFromRootSpan(spanEnd)

		if len(ends) != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", len(ends))
		}
		gc := withEffectiveDuration(ends[0])
		if d := gc.Session.DurationSeconds; d == nil || *d != 20 {
			t.Fatalf("effective duration = %v, want 20 (not inflated by the 5 s arrival delay)", d)
		}
	})

	t.Run("UC-041/A4 stale end: duration is start -> last game_active datapoint", func(t *testing.T) {
		clk := newEndClock()
		var ends []gamecontext.GameContext
		s := gamecontext.NewStore(time.Hour, time.Hour, clk.now)
		s.OnGameEnd = captureEnd(&ends)
		s.SetGameActive(true)
		clk.advance(12 * time.Second)
		s.SetGameActive(true) // last datapoint
		clk.advance(2 * time.Minute)
		s.EvictStale()

		if len(ends) != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", len(ends))
		}
		gc := withEffectiveDuration(ends[0])
		if d := gc.Session.DurationSeconds; d == nil || *d != 12 {
			t.Fatalf("effective duration = %v, want 12 (not inflated by the stale timeout)", d)
		}
	})
}

// countingFitness wraps a fitness func and counts how often it scored a game.
func countingFitness(n *int, f gameFitnessFunc) gameFitnessFunc {
	return func(gc gamecontext.GameContext, objective string) float64 {
		*n++
		return f(gc, objective)
	}
}

// UC: UC-041 A4
// UC: UC-043 main A11
func TestOnGameEnd_StaleEndExcludedFromExperiments(t *testing.T) {
	t.Run("UC-041/A4 stale-ended experiment game: slot released, no fitness folded", func(t *testing.T) {
		scored := 0
		spawner := &recordingSpawner{}
		resolve := func(context.Context) promote.Config {
			return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: false}
		}
		loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{},
			countingFitness(&scored, armFitness()))
		loop.fitnessStore = experiment.NewFitnessStore(0)
		id, err := loop.registry.Declare(experiment.Intent{
			FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
		})
		if err != nil {
			t.Fatalf("declare: %v", err)
		}
		if err := loop.registry.Start(context.Background(), id); err != nil {
			t.Fatalf("start: %v", err)
		}
		loop.registry.AllocateAndSpawn(context.Background())
		rec := spawner.drain()
		if len(rec) < 2 {
			t.Fatalf("expected a spawned pair, got %d games", len(rec))
		}

		stale := gcFor(rec[0].gameID, rec[0].experimentID, rec[0].arm)
		stale.EndReason = gamecontext.EndReasonStale
		loop.onGameEnd(stale)

		if scored != 0 {
			t.Fatalf("stale-ended game was scored %d times, want 0", scored)
		}
		if _, ok := loop.fitnessStore.Get(rec[0].gameID); ok {
			t.Fatal("stale-ended game recorded a finalized fitness sample")
		}
		if loop.registry.ReleaseGame(rec[0].gameID, "test") {
			t.Fatal("stale-ended game still held its experiment slot (not released)")
		}

		// Control: the sibling, ended normally, is scored and folded.
		normal := gcFor(rec[1].gameID, rec[1].experimentID, rec[1].arm)
		normal.EndReason = gamecontext.EndReasonZero
		endBoundGame(loop, normal)
		if scored != 1 {
			t.Fatalf("normally ended game scored %d times, want 1", scored)
		}
	})

	t.Run("UC-041/A4 stale-ended experiment game: released as stale and refilled", func(t *testing.T) {
		spawner := &recordingSpawner{}
		resolve := func(context.Context) promote.Config {
			return promote.Config{Mode: promote.ModeIssue, Target: promote.TargetLocal, Enabled: true}
		}
		loop := buildLoopForTest(t, spawner, resolve, &recordingRealDefault{}, &recordingGitHub{}, armFitness())
		id, err := loop.registry.Declare(experiment.Intent{
			FlagKey: "invincibility_seconds", ExperimentalValue: 6.0, Objective: "balanced", TargetNPerArm: 3,
		})
		if err != nil {
			t.Fatalf("declare: %v", err)
		}
		if err := loop.registry.Start(context.Background(), id); err != nil {
			t.Fatalf("start: %v", err)
		}
		loop.registry.AllocateAndSpawn(context.Background())
		rec := spawner.drain()
		if len(rec) == 0 {
			t.Fatal("expected a spawned game")
		}
		if loop.registry.AllocateAndSpawn(context.Background()) != 0 {
			t.Fatal("precondition: every slot should be in flight before the stale end")
		}

		stale := gcFor(rec[0].gameID, rec[0].experimentID, rec[0].arm)
		stale.EndReason = gamecontext.EndReasonStale
		loop.onGameEnd(stale)

		if note := staleReleaseNote(loop, id, rec[0].gameID); note != experiment.ReleaseReasonStale {
			t.Fatalf("release note = %q, want %q (BR-015: a stale rerun must not use up the budget)", note, experiment.ReleaseReasonStale)
		}
		if refill := spawner.drain(); len(refill) == 0 {
			t.Fatal("the freed slot was not refilled after the stale end")
		}
	})

	t.Run("UC-041/A4 stale-ended real game never seeds the recent-real baseline", func(t *testing.T) {
		store := experiment.NewFitnessStore(0)
		loop := loopWithStore(store)
		gc := realEnduranceGC("game_real_stale", 600)
		gc.EndReason = gamecontext.EndReasonStale
		loop.onGameEnd(gc)
		if store.Len() != 0 {
			t.Fatalf("stale-ended real game recorded %d fitness samples, want 0", store.Len())
		}
	})
}

// UC: UC-041 A4
// UC: UC-042 A6
func TestGameEndChain_StaleEndSkippedAndCounted(t *testing.T) {
	t.Run("UC-041/A4 stale end: counter increments, retro/summary/proposer skipped", func(t *testing.T) {
		reader := sdkmetric.NewManualReader()
		provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
		t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })

		consumed := 0
		chain := chainGameEnd(
			countStaleGameEnd(newGameEndStaleCounter(provider)),
			skipStaleGameEnd(func(gamecontext.GameContext) { consumed++ }),
		)
		for _, reason := range []string{gamecontext.EndReasonZero, gamecontext.EndReasonRootSpan, gamecontext.EndReasonStale} {
			chain(gamecontext.GameContext{SessionID: "game_" + reason, GameKind: "real", EndReason: reason})
		}

		if consumed != 2 {
			t.Errorf("consumer saw %d game ends, want 2 (zero and root_span, not stale)", consumed)
		}
		if got := staleCounterValue(t, reader, "primary"); got != 1 {
			t.Errorf("%s{game_kind=primary} = %d, want 1", metricGameEndStale, got)
		}
	})
}

// UC: UC-041 A4 main
// UC: UC-042 A6
func TestBuildGameEndChain_StaleEndReachesNoAnalysisConsumer(t *testing.T) {
	newChain := func() (func(gamecontext.GameContext), map[string][]string) {
		seen := map[string][]string{}
		record := func(name string) func(gamecontext.GameContext) {
			return func(c gamecontext.GameContext) { seen[name] = append(seen[name], c.EndReason) }
		}
		counter := newGameEndStaleCounter(metricnoopProvider())
		return buildGameEndChain(counter,
			record("experiment"), record("retro"), record("summary"), record("proposer")), seen
	}

	t.Run("UC-041/A4 stale end reaches the experiment hook only", func(t *testing.T) {
		chain, seen := newChain()
		chain(gamecontext.GameContext{SessionID: "game_stale", EndReason: gamecontext.EndReasonStale})
		if len(seen["experiment"]) != 1 {
			t.Errorf("experiment hook saw %d stale ends, want 1 (it releases the slot itself)", len(seen["experiment"]))
		}
		for _, name := range []string{"retro", "summary", "proposer"} {
			if len(seen[name]) != 0 {
				t.Errorf("%s saw a stale end, want none", name)
			}
		}
	})

	t.Run("UC-041/main step 7 consumers run in order: experiment, retro, summary, proposer", func(t *testing.T) {
		var order []string
		record := func(name string) func(gamecontext.GameContext) {
			return func(gamecontext.GameContext) { order = append(order, name) }
		}
		chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()),
			record("experiment"), record("retro"), record("summary"), record("proposer"))
		chain(gamecontext.GameContext{SessionID: "game_order", EndReason: gamecontext.EndReasonZero})
		want := []string{"experiment", "retro", "summary", "proposer"}
		if strings.Join(order, ",") != strings.Join(want, ",") {
			t.Fatalf("consumer order = %v, want %v", order, want)
		}
	})

	for _, reason := range []string{gamecontext.EndReasonZero, gamecontext.EndReasonRootSpan} {
		t.Run("UC-041/A4 "+reason+" end reaches every consumer", func(t *testing.T) {
			chain, seen := newChain()
			chain(gamecontext.GameContext{SessionID: "game_" + reason, EndReason: reason})
			for _, name := range []string{"experiment", "retro", "summary", "proposer"} {
				if len(seen[name]) != 1 {
					t.Errorf("%s saw %d %s ends, want 1", name, len(seen[name]), reason)
				}
			}
		})
	}
}

// staleReleaseNote returns the game_released note of gameID, or "" when none.
func staleReleaseNote(loop *experimentLoop, id, gameID string) string {
	cv, _ := loop.registry.CompactView(id)
	for _, ev := range cv.RecentTail {
		if ev.GameID == gameID && ev.Kind == journal.KindGameReleased {
			return ev.Note
		}
	}
	return ""
}

// metricnoopProvider is a meter provider that records nothing.
func metricnoopProvider() otelmetric.MeterProvider { return metricnoop.NewMeterProvider() }

// staleCounterValue collects agent_game_end_stale_total for one game_kind.
func staleCounterValue(t *testing.T, reader *sdkmetric.ManualReader, kind string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, md := range sm.Metrics {
			if md.Name != metricGameEndStale {
				continue
			}
			for _, dp := range md.Data.(metricdata.Sum[int64]).DataPoints {
				if v, ok := dp.Attributes.Value("game_kind"); ok && v.AsString() == kind {
					return dp.Value
				}
			}
		}
	}
	return 0
}
