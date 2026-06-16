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
// parenting them into one trace, so Slice A adds OTel Links back to the fire cycle.
//
// THE PRODUCTION TRUTH (the defect these tests now pin): the inbound ctx OnEvaluate gets
// carries NO active span (no otelgrpc interceptor; the sync agent.signal_received root is
// born only on the decided path, AFTER decide() returns — an async-fire cycle returns nil
// and never reaches it). So fireInferAsync CREATES the anchor agent.signal_received span
// itself at fire time and threads its (always-valid) SpanContext into the async job; BOTH
// agent.llm.infer.call and agent.llm.apply Link back to that anchor. The earlier Slice A
// captured trace.SpanContextFromContext(ctx) at the fire site, which was always invalid in
// production → the Links were a permanent no-op and the fire cycle had no trace presence.
// TestAsyncContinuity_ProductionPathAnchored is the regression that would have caught it.

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

// TestAsyncContinuity_ProductionPathAnchored is the Slice A core assertion AND the
// regression for the original defect: driven by the PRODUCTION fire-time default
// (context.Background(), no active span), an async-fire cycle must
//   - create exactly one anchor agent.signal_received span (the fire cycle is NOT idle —
//     it dispatched a decision, so it gets a real, navigable trace root), and
//   - have BOTH the agent.llm.infer.call and agent.llm.apply roots carry a fire_cycle Link
//     back to THAT anchor's exact SpanContext.
//
// The earlier Slice A captured trace.SpanContextFromContext(ctx) at the fire site; with no
// span active in that ctx (the production reality) the SpanContext was invalid → no anchor,
// no Links, the fire cycle invisible. This test is handed exactly that ctx and proves the
// anchor now exists and is the valid Link target, stitching the three disjoint async traces
// into one navigable whole while keeping each on its own root (no parenting; #917 preserved).
func TestAsyncContinuity_ProductionPathAnchored(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	// context.Background() carries NO span — the production fire-time default that the
	// original capture-from-ctx approach silently failed on.
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	// The anchor agent.signal_received span exists for the fire cycle and is valid.
	anchors := spansByName(sr.Ended(), SignalReceived)
	if len(anchors) != 1 {
		t.Fatalf("%s anchor spans = %d, want 1 (the async-fire cycle must produce one anchor root)", SignalReceived, len(anchors))
	}
	anchorSC := anchors[0].SpanContext()
	if !anchorSC.IsValid() {
		t.Fatalf("%s anchor SpanContext is invalid; the async roots have nothing valid to link to", SignalReceived)
	}
	// It is a real server-kind signal_received span carrying the session/game attribution.
	if anchors[0].SpanKind() != trace.SpanKindServer {
		t.Errorf("anchor span kind = %v, want Server", anchors[0].SpanKind())
	}
	if v, ok := attrValue(anchors[0], AttrGameID); !ok || v.AsString() != "g1" {
		t.Errorf("anchor span %s = %v (ok=%v), want g1", AttrGameID, v.AsString(), ok)
	}

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		lk, ok := linkToFireCycle(spans[0].Links())
		if !ok {
			t.Fatalf("%s carries no fire_cycle Link; want one back to the anchor signal_received trace", name)
		}
		if got := lk.SpanContext.TraceID(); got != anchorSC.TraceID() {
			t.Errorf("%s fire_cycle Link trace_id = %s, want %s (the anchor span)", name, got, anchorSC.TraceID())
		}
		if !lk.SpanContext.IsValid() {
			t.Errorf("%s fire_cycle Link must target a valid SpanContext", name)
		}
		// The Link carries the fire trace_id as a queryable attribute so a reader knows
		// what it points at even on backends that don't surface Link span contexts.
		var foundTraceAttr bool
		for _, kv := range lk.Attributes {
			if string(kv.Key) == attrLinkTraceID && kv.Value.AsString() == anchorSC.TraceID().String() {
				foundTraceAttr = true
			}
		}
		if !foundTraceAttr {
			t.Errorf("%s fire_cycle Link must carry %s=%s", name, attrLinkTraceID, anchorSC.TraceID())
		}
	}
}

// TestAsyncContinuity_NoAnchorOnPileUpSkip proves the anchor is created ONLY on a cycle
// that actually dispatches inference: when the pile-up guard is already claimed (an Infer
// is in flight for this game), fireInferAsync returns false WITHOUT firing — and must NOT
// create a stray anchor signal_received root for the skipped cycle.
func TestAsyncContinuity_NoAnchorOnPileUpSkip(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	// Simulate an in-flight call by claiming the pile-up guard, so this cycle's fire is
	// skipped and falls back to rules — no inference dispatched, hence no anchor.
	l.inflight.Store(true)
	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())

	if anchors := spansByName(sr.Ended(), SignalReceived); len(anchors) != 0 {
		t.Errorf("%s anchor spans = %d, want 0 (a pile-up-skipped cycle dispatches no inference, so no anchor)", SignalReceived, len(anchors))
	}
}

// TestAsyncContinuity_DecisionOutputUnchanged proves Slice A is observability-only: with
// the anchor + fire-cycle Link added, the async result still applies through the normal
// chain exactly as before — one dispatched action, apply span inference.applied=true.
func TestAsyncContinuity_DecisionOutputUnchanged(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
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
