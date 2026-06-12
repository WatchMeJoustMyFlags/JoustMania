package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// resolver_attribution_test.go drives the #741 resolver through the FULL decision
// loop (OnEvaluate) so the inference attribution triple — inference.configured,
// inference.used, inference.fallback_reason — is asserted exactly as it lands on
// the agent.decision span, and the coordination with the #847 gate is verified
// end-to-end (gate first; resolve only on admission).

// resolverLoop builds a Loop wired with a recording tracer, a metered gate
// counter, a fixed clock, a freshly-refreshed resolver, and a fixed-decision rules
// engine so a decision span always emits. The snapshot's model selects the chain
// top. Returns the loop, span recorder, and the three flippable backends.
func resolverLoop(t *testing.T, snap flags.Snapshot, cloud, gemma, phi bool, clock *time.Time) (*Loop, *tracetest.SpanRecorder, *fakeBackend, *fakeBackend, *fakeBackend) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	r, c, g, p := chainBackends(cloud, gemma, phi)
	r.Refresh(context.Background())

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}
	l.Actions = &fakeSink{}
	l.llmGated = newLLMGatedCounter(mp)
	l.SetLLMBudget(NewLLMBudget())
	l.SetResolver(r)
	l.now = func() time.Time { return *clock }
	return l, sr, c, g, p
}

// llmGateAdmit is an llm-mode snapshot whose #847 gate always admits (eligible for
// the test's game kind, no cadence floor, generous budget), with the given model
// selecting the resolver's chain top.
func llmGateAdmit(model string) flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 1.0},
		Capability:           flags.Capability{Model: model, PromptVariant: "balanced"},
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		LLMGate: flags.LLMGate{
			EligibleGameKinds:    []string{"real"},
			MinDecisionInterval:  0,
			MaxRequestsPerMinute: 100,
		},
	}
}

// inferenceTriple reads the three inference attributes off the single decision span.
func inferenceTriple(t *testing.T, sr *tracetest.SpanRecorder) (configured, used, fallback string) {
	t.Helper()
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1", len(decs))
	}
	cfg, _ := attrValue(decs[0], AttrInferenceConfigured)
	u, _ := attrValue(decs[0], AttrInferenceUsed)
	fb, ok := attrValue(decs[0], AttrInferenceFallback)
	if !ok {
		t.Fatal("inference.fallback_reason missing on decision span")
	}
	return cfg.AsString(), u.AsString(), fb.AsString()
}

// --- Acceptance #3: all three attributes present, including when configured == used ---

// TestAttribution_ConfiguredEqualsUsed: a reachable configured tier reports
// inference.used == the configured tier and an EMPTY fallback_reason (no
// degradation), with inference.configured = the model flag. All three present.
func TestAttribution_ConfiguredEqualsUsed(t *testing.T) {
	clock := time.Unix(1000, 0)
	// model=claude -> cloud tier; cloud up -> configured == used, no fallback.
	l, sr, _, _, _ := resolverLoop(t, llmGateAdmit("claude"), true, true, true, &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	configured, used, fallback := inferenceTriple(t, sr)
	if configured != "claude" {
		t.Errorf("inference.configured = %q, want claude", configured)
	}
	if used != TierCloud {
		t.Errorf("inference.used = %q, want %q (configured tier reachable)", used, TierCloud)
	}
	if fallback != "" {
		t.Errorf("inference.fallback_reason = %q, want empty (configured == used)", fallback)
	}
}

// TestAttribution_DegradesToLowerTier: cloud configured but down -> a lower tier
// serves, inference.used = that tier and fallback_reason = endpoint_unreachable.
func TestAttribution_DegradesToLowerTier(t *testing.T) {
	clock := time.Unix(1000, 0)
	// model=claude (cloud), cloud DOWN, gemma up -> used=gemma3:4b, endpoint_unreachable.
	l, sr, _, _, _ := resolverLoop(t, llmGateAdmit("claude"), false, true, true, &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	configured, used, fallback := inferenceTriple(t, sr)
	if configured != "claude" {
		t.Errorf("inference.configured = %q, want claude", configured)
	}
	if used != TierGemma {
		t.Errorf("inference.used = %q, want %q", used, TierGemma)
	}
	if fallback != FallbackEndpointUnreachable {
		t.Errorf("inference.fallback_reason = %q, want %q", fallback, FallbackEndpointUnreachable)
	}
}

// TestAttribution_WholeChainDownBottomsOutAtRules: every tier down -> used=rules,
// fallback=no_backend_available (the chain bottoms out, the system still decides).
func TestAttribution_WholeChainDownBottomsOutAtRules(t *testing.T) {
	clock := time.Unix(1000, 0)
	l, sr, _, _, _ := resolverLoop(t, llmGateAdmit("claude"), false, false, false, &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	_, used, fallback := inferenceTriple(t, sr)
	if used != InferenceRules {
		t.Errorf("inference.used = %q, want %q", used, InferenceRules)
	}
	if fallback != FallbackNoBackend {
		t.Errorf("inference.fallback_reason = %q, want %q", fallback, FallbackNoBackend)
	}
}

// --- Acceptance #4 (end-to-end): unplug mid-session, no stalled decisions ---

// TestAttribution_DegradesMidSession: across cycles, flipping the resolver's cache
// from cloud->gemma->rules changes inference.used each time with no error and a
// decision span on every cycle (no stall).
func TestAttribution_DegradesMidSession(t *testing.T) {
	clock := time.Unix(1000, 0)
	l, sr, c, g, _ := resolverLoop(t, llmGateAdmit("claude"), true, true, true, &clock)
	r := l.resolver
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	steps := []struct {
		name     string
		unplug   func()
		wantUsed string
	}{
		{"cloud up", func() {}, TierCloud},
		{"unplug cloud", func() { c.set(false); r.Refresh(context.Background()) }, TierGemma},
		{"unplug gemma", func() { g.set(false); r.Refresh(context.Background()) }, TierPhi},
	}
	for i, st := range steps {
		st.unplug()
		l.OnEvaluate(context.Background(), ctx, testTrigger())
		decs := spansByName(sr.Ended(), SpanDecision)
		if len(decs) != i+1 {
			t.Fatalf("step %q: decision spans = %d, want %d (no stalled decisions)", st.name, len(decs), i+1)
		}
		v, _ := attrValue(decs[i], AttrInferenceUsed)
		if v.AsString() != st.wantUsed {
			t.Errorf("step %q: inference.used = %q, want %q", st.name, v.AsString(), st.wantUsed)
		}
	}
}

// --- Acceptance #5 (end-to-end): recovery climbs back up ---

// TestAttribution_RecoversMidSession: with cloud down a cycle serves gemma; after
// cloud recovers and the cache refreshes, the next cycle reports cloud again.
func TestAttribution_RecoversMidSession(t *testing.T) {
	clock := time.Unix(1000, 0)
	l, sr, c, _, _ := resolverLoop(t, llmGateAdmit("claude"), false, true, true, &clock)
	r := l.resolver
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	l.OnEvaluate(context.Background(), ctx, testTrigger())
	if v, _ := attrValue(spansByName(sr.Ended(), SpanDecision)[0], AttrInferenceUsed); v.AsString() != TierGemma {
		t.Fatalf("first cycle used = %q, want %q (cloud down)", v.AsString(), TierGemma)
	}

	c.set(true)
	r.Refresh(context.Background())
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	decs := spansByName(sr.Ended(), SpanDecision)
	if v, _ := attrValue(decs[len(decs)-1], AttrInferenceUsed); v.AsString() != TierCloud {
		t.Errorf("after recovery, used = %q, want %q (climbed back up)", v.AsString(), TierCloud)
	}
}

// --- Acceptance #6: gate coordination — a gate-DENIED cycle is NOT overridden ---

// TestAttribution_GateDeniedKeepsGateReason: when the #847 gate DENIES (here:
// ineligible game kind), inference.used stays "rules" and inference.fallback_reason
// is the GATE reason (llm_not_eligible) — resolve_backend never runs, so even
// though the resolver's cloud tier is up, it does NOT override the gate attribution.
func TestAttribution_GateDeniedKeepsGateReason(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := llmGateAdmit("claude")
	snap.LLMGate.EligibleGameKinds = []string{"real"} // shadow will be gated
	// Cloud tier is UP and reachable — if resolve_backend wrongly ran on a denied
	// cycle it would report cloud; it must not.
	l, sr, _, _, _ := resolverLoop(t, snap, true, true, true, &clock)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "shadow"}, testTrigger())

	_, used, fallback := inferenceTriple(t, sr)
	if used != InferenceRules {
		t.Errorf("inference.used = %q, want %q (gate denied -> rules, resolver must not override)", used, InferenceRules)
	}
	if fallback != FallbackNotEligible {
		t.Errorf("inference.fallback_reason = %q, want %q (the gate reason, not a resolver reason)", fallback, FallbackNotEligible)
	}
	// And no prompt was captured (the gate skipped the whole attempt).
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 0 {
		t.Errorf("agent.llm.prompt spans = %d, want 0 (gate denied)", n)
	}
}

// TestAttribution_GateBudgetExhaustedKeepsGateReason: a budget-exhausted cycle
// (#847 layer 3) likewise keeps used=rules and fallback=llm_budget_exhausted even
// with a reachable resolver chain.
func TestAttribution_GateBudgetExhaustedKeepsGateReason(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := llmGateAdmit("claude")
	snap.LLMGate.MaxRequestsPerMinute = 1 // one slot, then exhausted
	l, sr, _, _, _ := resolverLoop(t, snap, true, true, true, &clock)
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	l.OnEvaluate(context.Background(), ctx, testTrigger()) // admits, spends the slot
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // budget exhausted

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 2 {
		t.Fatalf("decision spans = %d, want 2", len(decs))
	}
	// First cycle admitted -> resolved cloud. Second gated -> rules + budget reason.
	if v, _ := attrValue(decs[0], AttrInferenceUsed); v.AsString() != TierCloud {
		t.Errorf("cycle 1 used = %q, want %q (admitted, cloud reachable)", v.AsString(), TierCloud)
	}
	if v, _ := attrValue(decs[1], AttrInferenceUsed); v.AsString() != InferenceRules {
		t.Errorf("cycle 2 used = %q, want %q (gated)", v.AsString(), InferenceRules)
	}
	if v, _ := attrValue(decs[1], AttrInferenceFallback); v.AsString() != FallbackBudgetExhausted {
		t.Errorf("cycle 2 fallback = %q, want %q", v.AsString(), FallbackBudgetExhausted)
	}
}
