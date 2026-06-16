package decision

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// async_game_child_test.go is the #1178 async-path assertion suite: when the game-session
// trace ids are present, the async-materialized agent.llm.infer.call and agent.llm.apply
// spans are started as remote CHILDREN of the game span (they live in the game trace, with
// the game span as their parent) instead of own roots — while KEEPING the #1140 fire-cycle
// Link alongside. When the ids are absent the spans stay own-root + fire-cycle Link exactly
// as before (graceful fallback).

// activeGameTraceContext is activeContext enriched with the game-session trace ids, so the
// async path can re-parent its spans under the game span.
func activeGameTraceContext(gameID, serial string) gamecontext.GameContext {
	c := activeContext(gameID, serial)
	c.GameTraceID = hubGameTrace
	c.GameTraceSpanID = hubGameSpanID
	return c
}

// TestAsync_InferAndApplyChildrenOfGameSpan: with the game trace ids present, both
// agent.llm.infer.call and agent.llm.apply are remote children of the game span (their
// Parent is the game span and they live in the game trace), AND they still carry the
// fire-cycle Link back to the anchor signal_received.
func TestAsync_InferAndApplyChildrenOfGameSpan(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeGameTraceContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeGameTraceContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
	wantSID, _ := trace.SpanIDFromHex(hubGameSpanID)

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		s := spans[0]
		if s.Parent().TraceID() != wantTID {
			t.Errorf("%s parent trace_id = %s, want game trace %s", name, s.Parent().TraceID(), wantTID)
		}
		if s.Parent().SpanID() != wantSID {
			t.Errorf("%s parent span_id = %s, want game span %s", name, s.Parent().SpanID(), wantSID)
		}
		if s.SpanContext().TraceID() != wantTID {
			t.Errorf("%s must live in the game trace; got %s want %s", name, s.SpanContext().TraceID(), wantTID)
		}
		if !s.Parent().IsRemote() {
			t.Errorf("%s parent must be the REMOTE game span", name)
		}
		// The fire-cycle Link is KEPT alongside the new game-span parent.
		if _, ok := linkToFireCycle(s.Links()); !ok {
			t.Errorf("%s must keep its fire_cycle Link alongside the game-span parent", name)
		}
	}
}

// TestAsync_InferAndApplyOwnRootWhenIdsAbsent: with no game trace ids the async spans stay
// own-root (their parent is invalid — they share only the fire-cycle Link), exactly the
// pre-#1178 shape. infer.call and apply are on disjoint roots from each other.
func TestAsync_InferAndApplyOwnRootWhenIdsAbsent(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA")) // no game trace ids
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		if spans[0].Parent().IsValid() {
			t.Errorf("%s must be own-root when no game trace ids; parent=%v", name, spans[0].Parent())
		}
		// The fire-cycle Link remains the sole correlation, as before.
		if _, ok := linkToFireCycle(spans[0].Links()); !ok {
			t.Errorf("%s must still carry its fire_cycle Link when own-root", name)
		}
	}
}
