package decision

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// async_trace_continuity_test.go is the #1140 (Slice A) assertion suite: when LLM
// inference fires async (#917), the one logical decision is smeared across THREE
// disjoint trace roots — the fire cycle's agent.signal_received, the agent.llm.infer.call
// outbound-call root, and the agent.llm.apply root. The #917 non-blocking design forbids
// parenting them into one trace, so Slice A adds OTel Links: decide() captures the
// fire-cycle's active SpanContext, threads it (as a value) through the async job, and
// BOTH agent.llm.infer.call and agent.llm.apply add a Link back to it. These tests prove
// the Link is present when the fire-span SpanContext is valid, absent (graceful no-op)
// when it is invalid/missing, that no panic results, and that the decision output is
// unchanged by the observability addition.

// fireCycleCtx returns a context carrying a real, sampled span from the loop's tracer,
// so trace.SpanContextFromContext(ctx) inside decide() yields a VALID fire-cycle
// SpanContext to link back to. It returns the ctx and the SpanContext the Link targets.
// The span is ended immediately — a Link target is just a reference, not a live parent.
func fireCycleCtx(t *testing.T, l *Loop) (context.Context, trace.SpanContext) {
	t.Helper()
	ctx, span := l.Tracer.Start(context.Background(), "test.fire_cycle")
	span.End()
	return ctx, span.SpanContext()
}

// linkToFireCycle returns the Link on span whose link.kind=fire_cycle, and whether it was
// found. It is the reader's-eye view: a navigable reference back to the originating fire
// cycle's signal_received trace.
func linkToFireCycle(links []sdktrace.Link) (sdktrace.Link, bool) {
	for _, lk := range links {
		for _, kv := range lk.Attributes {
			if string(kv.Key) == attrLinkKind && kv.Value.AsString() == linkKindFireCycle {
				return lk, true
			}
		}
	}
	return sdktrace.Link{}, false
}

// TestAsyncContinuity_BothRootsLinkToFireCycle is the Slice A core assertion: with a
// valid fire-cycle SpanContext active in the ctx that OnEvaluate is handed, BOTH the
// agent.llm.infer.call and agent.llm.apply roots carry a Link back to that exact
// SpanContext — stitching the three disjoint async-decision traces into one navigable
// whole, while keeping each on its own root (no parenting; #917 preserved).
func TestAsyncContinuity_BothRootsLinkToFireCycle(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	fireCtx, fireSC := fireCycleCtx(t, l)
	l.OnEvaluate(fireCtx, activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		lk, ok := linkToFireCycle(spans[0].Links())
		if !ok {
			t.Fatalf("%s carries no fire_cycle Link; want one back to the signal_received trace", name)
		}
		if got := lk.SpanContext.TraceID(); got != fireSC.TraceID() {
			t.Errorf("%s fire_cycle Link trace_id = %s, want %s (the captured fire cycle)", name, got, fireSC.TraceID())
		}
		if !lk.SpanContext.IsValid() {
			t.Errorf("%s fire_cycle Link must target a valid SpanContext", name)
		}
		// The Link carries the fire trace_id as a queryable attribute so a reader knows
		// what it points at even on backends that don't surface Link span contexts.
		var foundTraceAttr bool
		for _, kv := range lk.Attributes {
			if string(kv.Key) == attrLinkTraceID && kv.Value.AsString() == fireSC.TraceID().String() {
				foundTraceAttr = true
			}
		}
		if !foundTraceAttr {
			t.Errorf("%s fire_cycle Link must carry %s=%s", name, attrLinkTraceID, fireSC.TraceID())
		}
	}
}

// TestAsyncContinuity_NoLinkWhenNoFireSpan is the graceful-fallback proof: in the
// production default the inbound ctx carries NO active span (there is no otelgrpc
// interceptor, and the agent.signal_received root is created only on the SYNC decided
// path, after decide() returns) — so the captured SpanContext is invalid and NEITHER
// async root gets a Link. No panic, spans created exactly as before.
func TestAsyncContinuity_NoLinkWhenNoFireSpan(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	// context.Background() carries no span — the production fire-time default.
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		if _, ok := linkToFireCycle(spans[0].Links()); ok {
			t.Errorf("%s carries a fire_cycle Link despite no active fire span; want none (graceful no-op)", name)
		}
	}
}

// TestAsyncContinuity_DecisionOutputUnchanged proves Slice A is observability-only: with
// the fire-cycle Link added, the async result still applies through the normal chain
// exactly as before — one dispatched action, apply span inference.applied=true.
func TestAsyncContinuity_DecisionOutputUnchanged(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	fireCtx, _ := fireCycleCtx(t, l)
	l.OnEvaluate(fireCtx, activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (Link addition must not change the decision)", sink.calls.Load())
	}
	apply := spansByName(sr.Ended(), SpanLLMApply)
	if len(apply) != 1 {
		t.Fatalf("agent.llm.apply spans = %d, want 1", len(apply))
	}
	if v, ok := attrValue(apply[0], "inference.applied"); !ok || !v.AsBool() {
		t.Error("apply span inference.applied must remain true for a fresh applied result")
	}
}

// TestFireCycleLink_Direct unit-tests the helper in isolation: an invalid (zero)
// SpanContext yields nil (no option, no link), a valid one yields exactly one option.
func TestFireCycleLink_Direct(t *testing.T) {
	if got := fireCycleLink(trace.SpanContext{}); got != nil {
		t.Errorf("fireCycleLink(invalid) = %v, want nil (graceful no-op)", got)
	}
	valid := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    trace.TraceID{0x01},
		SpanID:     trace.SpanID{0x01},
		TraceFlags: trace.FlagsSampled,
	})
	if got := fireCycleLink(valid); len(got) != 1 {
		t.Errorf("fireCycleLink(valid) = %d options, want 1", len(got))
	}
}
