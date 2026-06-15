package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// inference_flow_test.go is the #964 end-to-end proof: with a REAL inference backend
// configured (NewInferenceResolver) and reachable, an llm-mode cycle reaches mode=llm —
// the resolved inference tier is CALLED and its decision dispatched — even though the
// agent.model flag is the default phi4-mini. The inference span's gen_ai.request.model
// is the configured AGENT_INFERENCE_MODEL (single source of truth, gap 3), and an
// unreachable backend degrades to rules unchanged (graceful degradation preserved).

const flowModel = "gemma4:latest" // stands in for AGENT_INFERENCE_MODEL

// flowResponse is a valid in-vocab decision the configured backend returns.
const flowResponse = `{"intervention":"grant_shield","target_serial":"","value":"","reason":"llm decided","objective_served":"endurance"}`

// flowSnapshot is an llm-mode snapshot whose gate always admits. The model flag is the
// DEFAULT phi4-mini — the very value that, pre-#964, made the resolver start at the
// unreachable localhost tier and degrade to rules. With the inference tier wired it must
// no longer matter.
func flowSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 1.0},
		Capability:           flags.Capability{Model: "phi4-mini", PromptVariant: "balanced"},
		InterventionsAllowed: []string{"noop", "grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		LLMGate: flags.LLMGate{
			EligibleGameKinds:    []string{"real"},
			MinDecisionInterval:  0,
			MaxRequestsPerMinute: 100,
		},
	}
}

func flowLoop(t *testing.T, resolver *Resolver) (*Loop, *tracetest.SpanRecorder, *fakeSink) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	sink := &fakeSink{}
	l := NewLoop(&settableFlags{snap: flowSnapshot()}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	// Distinct rules output so a rules fallback is detectable by decision.reason.
	// grant_shield is in the allow-list so a rules decision actually dispatches (lets the
	// unreachable test assert the rules decision reached the sink); the LLM response uses a
	// distinct reason so the two paths are still distinguishable.
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "from rules"}}}
	l.Actions = sink
	l.SetLLMBudget(NewLLMBudget())
	l.SetResolver(resolver)
	return l, sr, sink
}

// TestInferenceFlow_ReachableBackendReachesModeLLM: the headline #964 acceptance — a
// reachable configured backend means the decision loop CALLS it (mode=llm) and
// dispatches the LLM's decision, with the configured model on the request span, despite
// the default phi4-mini agent.model flag.
func TestInferenceFlow_ReachableBackendReachesModeLLM(t *testing.T) {
	addr, closeFn := listenLocal(t)
	defer closeFn()

	delegateCalls := 0
	delegate := func(context.Context, llm.Prompt) (string, error) {
		delegateCalls++
		return flowResponse, nil
	}
	r := NewInferenceResolver(InferenceTier{Model: flowModel, Addr: addr, Infer: delegate},
		Endpoints{}, 0)
	r.Refresh(context.Background())

	l, sr, sink := flowLoop(t, r)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if delegateCalls != 1 {
		t.Fatalf("configured-backend Infer calls = %d, want 1 (mode=llm, backend CALLED)", delegateCalls)
	}
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "llm decided" {
		t.Errorf("decision.reason = %q, want the LLM's reason (mode=llm, not rules fallback)", v.AsString())
	}
	if v, ok := attrValue(decs[0], AttrInferenceUsed); !ok || v.AsString() != flowModel {
		t.Errorf("inference.used = %q, want %q (the configured inference tier decided)", v.AsString(), flowModel)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (llm decision dispatched)", sink.calls.Load())
	}
	// Gap 3: the inference span's request model is the configured AGENT_INFERENCE_MODEL,
	// not the agent.model flag (phi4-mini) — a single source of truth end to end.
	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if v, ok := attrValue(inf[0], "gen_ai.request.model"); !ok || v.AsString() != flowModel {
		t.Errorf("infer span gen_ai.request.model = %q, want %q (AGENT_INFERENCE_MODEL, not phi4-mini)", v.AsString(), flowModel)
	}
}

// TestInferenceFlow_UnreachableBackendStaysRules: an unreachable configured backend (and
// no legacy tier) degrades to rules — the dispatched decision is the rules engine's,
// inference.used=rules, no_backend_available. Graceful degradation is unchanged.
func TestInferenceFlow_UnreachableBackendStaysRules(t *testing.T) {
	delegate := func(context.Context, llm.Prompt) (string, error) {
		t.Fatal("delegate must not be called when the backend is unreachable")
		return "", nil
	}
	r := NewInferenceResolver(InferenceTier{Model: flowModel, Addr: "127.0.0.1:1", Infer: delegate},
		Endpoints{}, 0)
	r.Refresh(context.Background())

	l, sr, sink := flowLoop(t, r)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (rules decided)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "from rules" {
		t.Errorf("decision.reason = %q, want the rules fallback (mode=rules)", v.AsString())
	}
	if v, ok := attrValue(decs[0], AttrInferenceUsed); !ok || v.AsString() != InferenceRules {
		t.Errorf("inference.used = %q, want %q (degraded to rules)", v.AsString(), InferenceRules)
	}
	if v, ok := attrValue(decs[0], AttrInferenceFallback); !ok || v.AsString() != FallbackNoBackend {
		t.Errorf("inference.fallback_reason = %q, want %q", v.AsString(), FallbackNoBackend)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules decision dispatched)", sink.calls.Load())
	}
}

// TestInferenceFlow_DecisionSpanConfiguredReflectsBackend is the #1080 attribution half:
// the agent.decision span's inference.configured must report the CONFIGURED inference
// model (AGENT_INFERENCE_MODEL = gemma4:latest), not the stale agent.model flag
// (phi4-mini). It holds whether the backend is reachable (mode=llm) or unreachable
// (degraded to rules) — the configured attribution is independent of reachability.
// agent.model (AttrModel) still reports the raw flag, unchanged.
func TestInferenceFlow_DecisionSpanConfiguredReflectsBackend(t *testing.T) {
	cases := []struct {
		name      string
		reachable bool
	}{
		{"reachable", true},
		{"unreachable", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			delegate := func(context.Context, llm.Prompt) (string, error) { return flowResponse, nil }
			addr := "127.0.0.1:1" // unreachable by default
			if tc.reachable {
				var closeFn func()
				addr, closeFn = listenLocal(t)
				defer closeFn()
			}
			r := NewInferenceResolver(InferenceTier{Model: flowModel, Addr: addr, Infer: delegate}, Endpoints{}, 0)
			r.Refresh(context.Background())

			l, sr, _ := flowLoop(t, r)
			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

			decs := spansByName(sr.Ended(), SpanDecision)
			if len(decs) != 1 {
				t.Fatalf("agent.decision spans = %d, want 1", len(decs))
			}
			if v, ok := attrValue(decs[0], AttrInferenceConfigured); !ok || v.AsString() != flowModel {
				t.Errorf("inference.configured = %q, want %q (AGENT_INFERENCE_MODEL, not phi4-mini flag)", v.AsString(), flowModel)
			}
			// agent.model still reports the raw capability flag — only inference.configured
			// reflects the configured backend.
			if v, ok := attrValue(decs[0], AttrModel); !ok || v.AsString() != "phi4-mini" {
				t.Errorf("agent.model = %q, want the raw flag %q", v.AsString(), "phi4-mini")
			}
		})
	}
}
