package decision

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// --- LoopSet lifecycle: lazy create, Retain, Len ---

func TestLoopSet_ForLazyCreatesOncePerID(t *testing.T) {
	var created int32
	ls := NewLoopSet(func(string) *Loop {
		atomic.AddInt32(&created, 1)
		return NewLoop(nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	})

	a1 := ls.For("game-A")
	a2 := ls.For("game-A")
	b1 := ls.For("game-B")

	if a1 != a2 {
		t.Error("For(game-A) returned different Loops; must be lazily created once")
	}
	if a1 == b1 {
		t.Error("For(game-A) and For(game-B) returned the same Loop; must be per-game")
	}
	if got := atomic.LoadInt32(&created); got != 2 {
		t.Fatalf("factory invoked %d times, want 2 (one per distinct id)", got)
	}
	if ls.Len() != 2 {
		t.Fatalf("Len = %d, want 2", ls.Len())
	}
}

func TestLoopSet_RetainDropsNonActiveKeepsFallback(t *testing.T) {
	ls := newCountingLoopSet(nil)

	fallback := ls.For(gamecontext.FallbackGameID)
	ls.For("game-A")
	ls.For("game-B")
	if ls.Len() != 3 {
		t.Fatalf("Len = %d, want 3", ls.Len())
	}

	// Only game-A is still active; game-B is gone, fallback is implicitly permanent.
	ls.Retain(map[string]struct{}{"game-A": {}})

	if ls.Len() != 2 {
		t.Fatalf("Len after Retain = %d, want 2 (game-A + fallback, game-B dropped)", ls.Len())
	}
	// Fallback loop must be the SAME instance (never dropped, even when absent from active).
	if ls.For(gamecontext.FallbackGameID) != fallback {
		t.Error("fallback loop was dropped/recreated by Retain; it must be permanent")
	}
}

func TestLoopSet_RetainRecreatesOnNextFor(t *testing.T) {
	ls := newCountingLoopSet(nil)

	first := ls.For("game-A")
	ls.Retain(map[string]struct{}{}) // game-A not active -> dropped
	if ls.Len() != 0 {
		t.Fatalf("Len after dropping game-A = %d, want 0", ls.Len())
	}

	// A resumed game lazily recreates a FRESH loop (fresh budget).
	second := ls.For("game-A")
	if first == second {
		t.Error("recreated loop is the same instance; Retain must have dropped it")
	}
	if ls.Len() != 1 {
		t.Fatalf("Len after recreate = %d, want 1", ls.Len())
	}
}

// --- Per-game budget independence (the headline #845 AC) ---

// TestLoopSet_PerGameBudgetIndependence drives game A's loop until its per-minute
// budget is exhausted (decisions blocked with ReasonRateLimit), and asserts game
// B's loop — drawn from the SAME LoopSet — still dispatches unblocked: the budget
// is per-game, not global.
func TestLoopSet_PerGameBudgetIndependence(t *testing.T) {
	clock := time.Unix(2000, 0)
	// Budget of 2 hard interventions/min (eliminate_player costs 2).
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 2},
	}
	ls := NewLoopSet(func(string) *Loop {
		l := NewLoop(fakeFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
		l.Rules = &fixedRules{out: []Decision{{Intervention: "grant_shield"}}} // cost 1
		l.Actions = &recordingActions{}
		l.now = func() time.Time { return clock }
		return l
	})

	a := ls.For("game-A")
	// First two dispatches exhaust A's budget (2 x cost 1 = 2).
	if s := a.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Dispatched != 1 {
		t.Fatalf("A cycle 1 dispatched=%d, want 1", s.Dispatched)
	}
	if s := a.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Dispatched != 1 {
		t.Fatalf("A cycle 2 dispatched=%d, want 1", s.Dispatched)
	}
	// A is now over budget: the next decision is blocked with ReasonRateLimit.
	s := a.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())
	if s.Blocked != 1 || len(s.Outcomes) != 1 || s.Outcomes[0].BlockReason != ReasonRateLimit {
		t.Fatalf("A over-budget cycle: blocked=%d outcomes=%+v, want 1 blocked w/ ReasonRateLimit", s.Blocked, s.Outcomes)
	}

	// Game B's loop has an UNTOUCHED budget — it must still dispatch.
	b := ls.For("game-B")
	if s := b.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Dispatched != 1 || s.Blocked != 0 {
		t.Fatalf("B dispatched=%d blocked=%d, want 1/0 (B's budget is independent of A)", s.Dispatched, s.Blocked)
	}
}

// --- Per-game throttle independence ---

// TestLoopSet_PerGameThrottleIndependence: a single rapid cycle on each of two
// games each emits its own throttled trace within the same throttle interval —
// game A consuming its throttle slot does not silence game B.
func TestLoopSet_PerGameThrottleIndependence(t *testing.T) {
	clock := time.Unix(3000, 0)
	snap := flags.Snapshot{Enabled: false} // disabled -> throttled agent.disabled span

	newRecorder := func() *tracetest.SpanRecorder {
		return tracetest.NewSpanRecorder()
	}
	recorders := map[string]*tracetest.SpanRecorder{}
	var mu sync.Mutex

	ls := NewLoopSet(func(gameID string) *Loop {
		sr := newRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		l := NewLoop(fakeFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
		l.Tracer = tp.Tracer("test")
		l.now = func() time.Time { return clock }
		l.SetThrottle(time.Minute) // wide throttle: only the FIRST cycle per loop emits
		mu.Lock()
		recorders[gameID] = sr
		mu.Unlock()
		return l
	})

	// Two rapid cycles per game at the SAME instant. With a shared throttle the
	// second game would be silenced; with per-game throttle each emits exactly once.
	for _, id := range []string{"game-A", "game-B"} {
		l := ls.For(id)
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: id}, testTrigger())
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: id}, testTrigger())
	}

	for _, id := range []string{"game-A", "game-B"} {
		disabled := spansByName(recorders[id].Ended(), SpanDisabled)
		if len(disabled) != 1 {
			t.Errorf("%s emitted %d agent.disabled spans, want 1 (own throttle slot)", id, len(disabled))
		}
	}
}

// --- Rules-engine isolation (race-prevention proof; run with -race) ---

// TestLoopSet_RulesEngineIsolation: two games with DIFFERENT objective weights,
// each driven through its own loop, concurrently. Two things are proven:
//
//  1. Attribution: each game's decision spans carry ONLY that game's
//     agent.objectives — game A's endurance weights never appear on game B's span.
//  2. No shared-engine race: each loop is built with a FRESH ObjectiveRules engine
//     (NewObjectiveRulesLive, exactly as main.go's factory), which the loop drives
//     per cycle via SetObjectives / SetFitness / LastFitness. Run under -race, a
//     shared engine would flag a data race on that per-cycle state; fresh engines
//     do not contend.
func TestLoopSet_RulesEngineIsolation(t *testing.T) {
	recorders := map[string]*tracetest.SpanRecorder{}
	var rmu sync.Mutex

	objectivesByGame := map[string]map[string]float64{
		"game-A": {"endurance": 1.0},
		"game-B": {"accelerate": 1.0},
	}

	ls := NewLoopSet(func(gameID string) *Loop {
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		fl := &settableFlags{snap: flags.Snapshot{
			Enabled:              true,
			Mode:                 "rules",
			Objectives:           objectivesByGame[gameID],
			InterventionsAllowed: []string{"grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}}
		l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
		// FRESH real engine per loop — the racy component. The two goroutines below
		// each drive their OWN engine's SetObjectives/SetFitness/LastFitness; a
		// shared engine would race under -race. A canned-decision wrapper is layered
		// on so attribution is deterministic regardless of the ruleset's candidate
		// selection, while the real engine's per-cycle publish path still runs.
		l.Rules = &drivenEngine{
			real: NewObjectiveRulesLive(nil),
			out:  []Decision{{Intervention: "grant_shield"}},
		}
		l.Tracer = tp.Tracer("test")
		rmu.Lock()
		recorders[gameID] = sr
		rmu.Unlock()
		return l
	})

	ctxFor := func(id string) gamecontext.GameContext {
		return gamecontext.GameContext{
			SessionID: id,
			Players: map[string]*gamecontext.PlayerSignals{
				"P1": {Serial: "P1", Active: ptrB(true)},
				"P2": {Serial: "P2", Active: ptrB(true)},
			},
			Session: gamecontext.SessionSignals{DurationSeconds: ptrF(120)},
		}
	}

	var wg sync.WaitGroup
	for _, id := range []string{"game-A", "game-B"} {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			l := ls.For(id)
			for i := 0; i < 20; i++ {
				l.OnEvaluate(context.Background(), ctxFor(id), testTrigger())
			}
		}(id)
	}
	wg.Wait()

	// Each game's decision spans must carry ONLY that game's objective weight.
	wantObjectives := map[string]string{"game-A": "endurance=1", "game-B": "accelerate=1"}
	for _, id := range []string{"game-A", "game-B"} {
		decisions := spansByName(recorders[id].Ended(), SpanDecision)
		if len(decisions) == 0 {
			t.Fatalf("%s emitted no decision spans", id)
		}
		for _, d := range decisions {
			v, ok := attrValue(d, AttrObjectives)
			if !ok {
				t.Fatalf("%s decision span missing agent.objectives", id)
			}
			if got := v.AsString(); got != wantObjectives[id] {
				t.Errorf("%s decision span agent.objectives=%q, want %q (engine isolation breached)", id, got, wantObjectives[id])
			}
		}
	}
}

// drivenEngine wraps a real *ObjectiveRules so the per-cycle publish/read path
// (SetObjectives/SetFitness/LastFitness — the racy state under test) runs on every
// cycle, while returning a deterministic decision set so span attribution does not
// depend on the ruleset's candidate selection. It satisfies objectivePublisher,
// fitnessPublisher, and fitnessReader by delegating to the wrapped engine.
type drivenEngine struct {
	real *ObjectiveRules
	out  []Decision
}

func (e *drivenEngine) Evaluate(ctx context.Context, c gamecontext.GameContext) []Decision {
	_ = e.real.Evaluate(ctx, c) // exercise the real engine's racy per-cycle state
	return e.out
}

func (e *drivenEngine) SetObjectives(w map[string]float64) { e.real.SetObjectives(w) }
func (e *drivenEngine) SetFitness(t FitnessThresholds)     { e.real.SetFitness(t) }
func (e *drivenEngine) LastFitness() FitnessEvaluation     { return e.real.LastFitness() }

func ptrB(b bool) *bool { return &b }

// --- Shadow-vs-real attribution ---

// TestLoopSet_ShadowVsRealAttribution: two partitions, one real and one shadow,
// each driven through its own loop. The decision spans must carry the right
// game.kind per game.
func TestLoopSet_ShadowVsRealAttribution(t *testing.T) {
	recorders := map[string]*tracetest.SpanRecorder{}

	kindByGame := map[string]string{"game-real": "real", "game-shadow": "shadow"}

	ls := NewLoopSet(func(gameID string) *Loop {
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		fl := &settableFlags{snap: flags.Snapshot{
			Enabled:              true,
			Mode:                 "rules",
			InterventionsAllowed: []string{"grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}}
		l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
		l.Rules = &fixedRules{out: []Decision{{Intervention: "grant_shield"}}}
		l.Tracer = tp.Tracer("test")
		recorders[gameID] = sr
		return l
	})

	for id, kind := range kindByGame {
		l := ls.For(id)
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: id, GameKind: kind}, testTrigger())
	}

	for id, wantKind := range kindByGame {
		decisions := spansByName(recorders[id].Ended(), SpanDecision)
		if len(decisions) != 1 {
			t.Fatalf("%s emitted %d decision spans, want 1", id, len(decisions))
		}
		v, ok := attrValue(decisions[0], AttrGameKind)
		if !ok {
			t.Fatalf("%s decision span missing game.kind", id)
		}
		if got := v.AsString(); got != wantKind {
			t.Errorf("%s decision span game.kind=%q, want %q", id, got, wantKind)
		}
		// game.id alias must equal the partition's game id on the root span.
		roots := spansByName(recorders[id].Ended(), SignalReceived)
		if len(roots) != 1 {
			t.Fatalf("%s emitted %d root spans, want 1", id, len(roots))
		}
		gid, ok := attrValue(roots[0], AttrGameID)
		if !ok || gid.AsString() != id {
			t.Errorf("%s root span game.id=%q (ok=%v), want %q", id, gid.AsString(), ok, id)
		}
	}
}

// newCountingLoopSet builds a LoopSet whose factory makes inert disabled loops,
// optionally recording each created gameID into seen.
func newCountingLoopSet(seen *[]string) *LoopSet {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	return NewLoopSet(func(gameID string) *Loop {
		if seen != nil {
			*seen = append(*seen, gameID)
		}
		return NewLoop(nil, logger)
	})
}
