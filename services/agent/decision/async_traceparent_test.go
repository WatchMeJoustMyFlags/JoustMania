package decision

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/llm"
)

// async_traceparent_test.go is the #1096 async-follow-up assertion, mirroring the sync
// path's inference/openai_test.go TestInfer_InjectsTraceparentWithinActiveSpan but for
// the ASYNC production path (#917 async_infer.go runInfer). On the sync path the
// agent.llm.infer span is active in ctx when Infer runs, so #1112's openai.go injection
// parents the litellm gateway's gen_ai span under it. On the async path the ATTRIBUTION
// agent.llm.infer span is emitted later, at apply time — so runInfer now opens a minimal
// agent.llm.infer.call client span JUST around the outbound call, giving the HTTP call's
// ctx an active, sampled span for the injector to read. These tests prove that ctx
// carries an active span (a traceparent is injectable) AND that the apply-time
// attribution span is left unchanged.

// withTraceContextPropagator installs the W3C TraceContext propagator as the global for
// the test duration, restoring it afterward — same helper shape as the sync test, so the
// injection path behaves exactly as production.
func withTraceContextPropagator(t *testing.T) {
	t.Helper()
	prev := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(prev) })
}

// injectingBackend stands in for the real inference.OpenAIBackend on the async path: its
// Infer does EXACTLY what openai.go does for propagation — Inject the active span from
// the ctx it was handed into HTTP headers — and records the resulting traceparent plus
// the span context it observed. The decision package depends only on the narrow Backend
// seam, so this in-package fake exercises the propagation contract without a real socket.
type injectingBackend struct {
	name           string
	response       string
	gotTraceparent string
	sawSpan        bool
	observedTrace  trace.TraceID
}

func (b *injectingBackend) Name() string                   { return b.name }
func (b *injectingBackend) Available(context.Context) bool { return true }

func (b *injectingBackend) Infer(ctx context.Context, _ llm.Prompt) (string, error) {
	// Mirror inference/openai.go: inject the active span from THIS ctx into headers.
	hdr := http.Header{}
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(hdr))
	b.gotTraceparent = hdr.Get("traceparent")

	sc := trace.SpanContextFromContext(ctx)
	b.sawSpan = sc.IsValid()
	b.observedTrace = sc.TraceID()
	return b.response, nil
}

// TestAsync_InjectsTraceparentWithinActiveSpan is the #1096 async core assertion: when
// the async path fires backend.Infer, the call's ctx MUST carry an active (sampled) span
// so the openai.go injector emits a traceparent the litellm gateway can nest its gen_ai
// span under. Without the agent.llm.infer.call wrapper this header would be absent (no
// span is active in runInfer's ctx — the attribution span is emitted later at apply
// time), so this is the direct regression guard for the async gap #1096 calls out.
func TestAsync_InjectsTraceparentWithinActiveSpan(t *testing.T) {
	withTraceContextPropagator(t)

	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	if !be.sawSpan {
		t.Fatal("async Infer ctx carried no active span; openai.go would inject no traceparent (the #1096 async gap)")
	}
	if be.gotTraceparent == "" {
		t.Fatal("no traceparent injected on the async inference call despite an active span; want one")
	}

	// The injected traceparent must carry the agent.llm.infer.call span's trace-id, so
	// the gateway span nests under THIS trace (not an orphan / a different trace).
	callSpans := spansByName(sr.Ended(), SpanLLMInferCall)
	if len(callSpans) != 1 {
		t.Fatalf("agent.llm.infer.call spans = %d, want 1 (the outbound-call wrapper)", len(callSpans))
	}
	wantTraceID := callSpans[0].SpanContext().TraceID().String()
	if !strings.Contains(be.gotTraceparent, wantTraceID) {
		t.Errorf("traceparent %q does not carry the call span's trace-id %q", be.gotTraceparent, wantTraceID)
	}
	// The observed span context (the active span at HTTP-call time) shares that trace.
	if be.observedTrace.String() != wantTraceID {
		t.Errorf("active-span trace-id at Infer = %q, want the call span's %q", be.observedTrace, wantTraceID)
	}

	// The call wrapper is a CLIENT span (the outbound hop), carrying only the request
	// model — it must NOT duplicate the apply-time attribution span's gen_ai shape.
	if got := callSpans[0].SpanKind(); got != trace.SpanKindClient {
		t.Errorf("agent.llm.infer.call span kind = %v, want client", got)
	}
}

// TestAsync_TraceparentDoesNotDisturbApplyAttributionSpan guards the constraint that the
// outbound-call wrapper changes ONLY propagation: the deliberately apply-time
// agent.llm.infer attribution span (emitAsyncInferSpan) must still be emitted with its
// gen_ai attribution, its game.id, its latency, and the decision still applied — exactly
// as before #1096's async wrapper was added.
func TestAsync_TraceparentDoesNotDisturbApplyAttributionSpan(t *testing.T) {
	withTraceContextPropagator(t)

	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	// The apply-time attribution span is unchanged: present, with gen_ai model, game.id,
	// latency, and the model's reasoning — the wrapper added no second attribution span.
	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer (apply-time attribution) spans = %d, want exactly 1", len(inf))
	}
	if v, ok := attrValue(inf[0], "gen_ai.request.model"); !ok || v.AsString() != "phi4-mini" {
		t.Errorf("attribution span gen_ai.request.model = %q, want phi4-mini (attribution unchanged)", v.AsString())
	}
	if v, ok := attrValue(inf[0], AttrGameID); !ok || v.AsString() != "g1" {
		t.Errorf("attribution span %s = %q, want g1 (attribution unchanged)", AttrGameID, v.AsString())
	}
	if _, ok := attrValue(inf[0], AttrLLMLatencyMs); !ok {
		t.Error("attribution span missing inference.latency_ms (attribution unchanged)")
	}
	if v, ok := attrValue(inf[0], AttrDecisionAction); !ok || v.AsString() != "grant_shield" {
		t.Errorf("attribution span decision.action = %q, want grant_shield (decode unchanged)", v.AsString())
	}

	// The decision still applied through the normal chain — the wrapper did not alter the
	// outcome, only gave the outbound call a parent span.
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (fresh llm decision still dispatched)", sink.calls.Load())
	}
	apply := spansByName(sr.Ended(), SpanLLMApply)
	if len(apply) != 1 {
		t.Fatalf("agent.llm.apply spans = %d, want 1", len(apply))
	}
	if v, ok := attrValue(apply[0], "inference.applied"); !ok || !v.AsBool() {
		t.Error("apply span inference.applied must remain true for a fresh applied result")
	}
}

// TestAsync_NoTraceparentWithoutPropagatorSpan is the default-safe path: with the
// global propagator set but the loop's root context carrying no span (the production
// default — runInfer derives from rootCtx, not a per-cycle span), the agent.llm.infer.call
// wrapper still starts a span via the loop tracer, so a traceparent IS injectable. This
// asserts the wrapper is what provides the active span, independent of any ambient
// caller span — the whole point of the async fix.
func TestAsync_CallWrapperProvidesSpanIndependentOfCaller(t *testing.T) {
	withTraceContextPropagator(t)

	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, _, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)
	// Root context deliberately carries NO span (matches production: rootCtx is the
	// agent's root, not a request span). The wrapper must still make a span active.
	l.SetRootContext(context.Background())

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	if !be.sawSpan || be.gotTraceparent == "" {
		t.Fatal("call wrapper failed to provide an active span for injection on a span-less root ctx")
	}
}
