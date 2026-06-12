package decision

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// gateSnapshot is an llm-mode snapshot whose gate config the test sets explicitly.
// It is eligible for nothing by default — each test supplies its own LLMGate.
func gateSnapshot(gate flags.LLMGate) flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 1.0},
		Capability:           flags.Capability{Model: "phi4-mini", PromptVariant: "balanced"},
		InterventionsAllowed: []string{"noop", "grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		LLMGate:              gate,
	}
}

// gatedByReason collects the agent_llm_gated_total counter into a reason->count map.
func gatedByReason(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_llm_gated_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				reason, _ := dp.Attributes.Value("reason")
				out[reason.AsString()] = dp.Value
			}
		}
	}
	return out
}

// gateLoop builds a Loop wired with a recording tracer, a metered gate counter,
// a fixed clock, the given shared budget, and a fixed-decision rules engine so a
// decision span always emits (lets us read inference.fallback_reason back). It
// returns the loop, span recorder, and metric reader.
func gateLoop(t *testing.T, snap flags.Snapshot, budget *llmBudget, clock *time.Time) (*Loop, *tracetest.SpanRecorder, *metric.ManualReader) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}
	l.Actions = &fakeSink{}
	l.llmGated = newLLMGatedCounter(mp)
	l.SetLLMBudget(budget)
	// A fixed clock drives cadence/budget windows deterministically and keeps the
	// capture throttle open across the test's cycles.
	l.now = func() time.Time { return *clock }
	return l, sr, reader
}

// fallbackOnDecision reads inference.fallback_reason off the single decision span.
func fallbackOnDecision(t *testing.T, sr *tracetest.SpanRecorder) string {
	t.Helper()
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1", len(decs))
	}
	v, ok := attrValue(decs[0], AttrInferenceFallback)
	if !ok {
		t.Fatal("inference.fallback_reason missing on decision span")
	}
	return v.AsString()
}

// --- Acceptance #2: shadow games produce zero llm attempts with default flags ---

// TestGate_ShadowGameNotEligible: a shadow game under the default ["real"]
// eligibility never captures a prompt; the decision span carries llm_not_eligible
// and the gated counter increments for that reason.
func TestGate_ShadowGameNotEligible(t *testing.T) {
	clock := time.Unix(1000, 0)
	// Default-equivalent gate: eligible only for "real".
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  10 * time.Second,
		MaxRequestsPerMinute: 6,
	})
	l, sr, reader := gateLoop(t, snap, NewLLMBudget(), &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "shadow"}, testTrigger())

	// NO prompt capture for an ineligible game.
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 0 {
		t.Errorf("agent.llm.prompt spans = %d, want 0 for an ineligible shadow game", n)
	}
	if got := fallbackOnDecision(t, sr); got != FallbackNotEligible {
		t.Errorf("inference.fallback_reason = %q, want %q", got, FallbackNotEligible)
	}
	if got := gatedByReason(t, reader); got[FallbackNotEligible] != 1 {
		t.Errorf("agent_llm_gated_total{reason=%s} = %d, want 1", FallbackNotEligible, got[FallbackNotEligible])
	}
}

// --- Acceptance #1 + #4: ordering and per-reason fallback attribution ---

// TestGate_EligibleAllowsAndCaptures: an eligible real game with budget available
// passes the gate — the prompt is captured and the fallback is the unchanged
// no_backend_available (gate ALLOWED).
func TestGate_EligibleAllowsAndCaptures(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  10 * time.Second,
		MaxRequestsPerMinute: 6,
	})
	l, sr, reader := gateLoop(t, snap, NewLLMBudget(), &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 1 {
		t.Errorf("agent.llm.prompt spans = %d, want 1 (gate admitted)", n)
	}
	if got := fallbackOnDecision(t, sr); got != FallbackNoBackend {
		t.Errorf("inference.fallback_reason = %q, want %q (allowed, no backend)", got, FallbackNoBackend)
	}
	if got := gatedByReason(t, reader); len(got) != 0 {
		t.Errorf("agent_llm_gated_total has entries %v, want none (nothing gated)", got)
	}
}

// TestGate_CadenceBlocksSecondAttempt: a second attempt for the same game within
// the cadence interval is gated llm_interval — and does NOT capture a prompt.
func TestGate_CadenceBlocksSecondAttempt(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  10 * time.Second,
		MaxRequestsPerMinute: 100,
	})
	l, sr, reader := gateLoop(t, snap, NewLLMBudget(), &clock)
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	// First attempt admitted.
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	// Second attempt 3s later — inside the 10s cadence floor.
	clock = clock.Add(3 * time.Second)
	l.OnEvaluate(context.Background(), ctx, testTrigger())

	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 1 {
		t.Errorf("agent.llm.prompt spans = %d, want 1 (second blocked by cadence)", n)
	}
	if got := gatedByReason(t, reader); got[FallbackInterval] != 1 {
		t.Errorf("agent_llm_gated_total{reason=%s} = %d, want 1", FallbackInterval, got[FallbackInterval])
	}

	// Once the interval elapses, the next attempt is admitted again.
	clock = clock.Add(10 * time.Second)
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 2 {
		t.Errorf("agent.llm.prompt spans = %d, want 2 (cadence elapsed, re-admitted)", n)
	}
}

// TestGate_BudgetExhausted: with no cadence floor, a single game exhausts the
// global budget; the next attempt is gated llm_budget_exhausted.
func TestGate_BudgetExhausted(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  0, // no cadence floor
		MaxRequestsPerMinute: 2,
	})
	l, _, reader := gateLoop(t, snap, NewLLMBudget(), &clock)
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 1 admitted
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 2 admitted
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 3 -> budget exhausted

	if got := gatedByReason(t, reader); got[FallbackBudgetExhausted] != 1 {
		t.Errorf("agent_llm_gated_total{reason=%s} = %d, want 1", FallbackBudgetExhausted, got[FallbackBudgetExhausted])
	}
}

// TestGate_OrderEligibilityFirst: an ineligible game NEVER touches cadence or the
// budget — even with a zero budget, the reason is llm_not_eligible (the first
// layer short-circuits), and the shared budget admits a later eligible game fully.
func TestGate_OrderEligibilityFirst(t *testing.T) {
	clock := time.Unix(1000, 0)
	shared := NewLLMBudget()
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  0,
		MaxRequestsPerMinute: 1, // tiny budget
	})
	l, sr, reader := gateLoop(t, snap, shared, &clock)

	// Ineligible shadow game: gated at layer 1, budget untouched.
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "shadow"}, testTrigger())
	if got := gatedByReason(t, reader); got[FallbackNotEligible] != 1 || got[FallbackBudgetExhausted] != 0 {
		t.Errorf("gated map = %v, want only llm_not_eligible (eligibility short-circuits before budget)", got)
	}
	// The eligible real game can still spend the single budget slot. Advance the
	// clock past the capture throttle (the shadow cycle consumed this interval's
	// slot) so the admitted real cycle actually emits its capture span.
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 0 {
		t.Fatalf("unexpected prompt capture before eligible attempt")
	}
	clock = clock.Add(2 * time.Second)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 1 {
		t.Errorf("agent.llm.prompt spans = %d, want 1 (real game spends the budget the shadow game never touched)", n)
	}
}

// --- Acceptance #5: flags hot-reload (tightening the budget mid-session) ---

// TestGate_BudgetHotReload: a budget admitted under a generous cap is denied on
// the next cycle once the flag tightens below the in-window count — proving the
// cap is read live per cycle, never cached.
func TestGate_BudgetHotReload(t *testing.T) {
	clock := time.Unix(1000, 0)
	fl := &settableFlags{snap: gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  0,
		MaxRequestsPerMinute: 5,
	})}
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))
	l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield"}}}
	l.Actions = &fakeSink{}
	l.llmGated = newLLMGatedCounter(mp)
	l.SetLLMBudget(NewLLMBudget())
	l.now = func() time.Time { return clock }
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	// Two admissions under cap 5 (advance past the 1s capture throttle between them,
	// staying well inside the 1-minute budget window so both stay counted).
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	clock = clock.Add(2 * time.Second)
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 2 {
		t.Fatalf("agent.llm.prompt spans = %d, want 2 under cap 5", n)
	}
	// Tighten the budget flag to 2 (== current window count): the very next cycle
	// is gated, proving the cap is read live (the budget window still holds 2).
	tightened := fl.snap
	tightened.LLMGate.MaxRequestsPerMinute = 2
	fl.snap = tightened
	clock = clock.Add(2 * time.Second)
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	if got := gatedByReason(t, reader); got[FallbackBudgetExhausted] != 1 {
		t.Errorf("agent_llm_gated_total{reason=%s} = %d, want 1 after mid-session tighten", FallbackBudgetExhausted, got[FallbackBudgetExhausted])
	}
}

// --- Acceptance #3: 4 concurrent games share one global budget ---

// TestGate_SharedBudgetAcrossLoops: four separate Loops (four games) all wired to
// ONE shared budget never admit more than max_requests_per_minute combined. The
// capture span is throttle-gated, so admissions are counted as (total cycles -
// gated denials) via a SHARED metric reader, not by capture-span count.
func TestGate_SharedBudgetAcrossLoops(t *testing.T) {
	clock := time.Unix(1000, 0)
	const cap = 6
	const games = 4
	const cyclesPerGame = 10
	shared := NewLLMBudget()

	// One shared meter provider so the gated counter aggregates across all loops.
	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))
	sharedCounter := newLLMGatedCounter(mp)

	makeLoop := func() *Loop {
		snap := gateSnapshot(flags.LLMGate{
			EligibleGameKinds:    []string{"real"},
			MinDecisionInterval:  0, // no cadence floor: the budget is the sole constraint
			MaxRequestsPerMinute: cap,
		})
		l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
		l.Tracer = sdktrace.NewTracerProvider().Tracer("test")
		l.Rules = fakeRules{out: nil} // idle: gate runs, no decision spans
		l.Actions = &fakeSink{}
		l.llmGated = sharedCounter
		l.SetLLMBudget(shared)
		l.now = func() time.Time { return clock }
		return l
	}

	loops := make([]*Loop, games)
	for i := range loops {
		loops[i] = makeLoop()
	}

	var wg sync.WaitGroup
	for i := range loops {
		l := loops[i]
		gameID := "game-" + string(rune('A'+i))
		wg.Add(1)
		go func() {
			defer wg.Done()
			for c := 0; c < cyclesPerGame; c++ {
				l.OnEvaluate(context.Background(),
					gamecontext.GameContext{SessionID: gameID, GameKind: "real"}, testTrigger())
			}
		}()
	}
	wg.Wait()

	// admitted = total cycles - gated denials. With no cadence and a budget of cap,
	// every denial is llm_budget_exhausted, so admitted must equal exactly cap and
	// can never exceed it.
	gated := gatedByReason(t, reader)
	totalGated := gated[FallbackBudgetExhausted] + gated[FallbackInterval] + gated[FallbackNotEligible]
	admitted := int64(games*cyclesPerGame) - totalGated
	if admitted > cap {
		t.Errorf("total admitted LLM attempts across %d games = %d, want <= %d (shared budget)", games, admitted, cap)
	}
	if admitted != cap {
		t.Errorf("total admitted = %d, want exactly %d (budget fully spent, never exceeded)", admitted, cap)
	}
	if gated[FallbackInterval] != 0 || gated[FallbackNotEligible] != 0 {
		t.Errorf("unexpected non-budget gating: %v (only budget should gate here)", gated)
	}
}
