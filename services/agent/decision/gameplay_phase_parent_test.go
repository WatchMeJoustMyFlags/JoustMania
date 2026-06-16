package decision

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// gameplay_phase_parent_test.go is the #1195 assertion suite (refines #1187): the
// agent's IN-GAME decision chain (signal_received -> decision -> action; the async
// infer.call + apply spans) re-parents under the coordinator's gameplay_phase span
// when its span_id is present, with a STRICT two-tier fallback so correlation is
// never lost:
//
//  1. gameplay_phase span_id present  -> child of gameplay_phase (the new target).
//  2. gameplay_phase absent, game root present -> child of game root (#1187 behavior).
//  3. both absent -> own-root (pre-#1187 shape).
//
// gameplay_phase is in the SAME trace as the game root (GameTraceID is identical),
// so the only thing that changes between tiers 1 and 2 is the parent span_id.

// A distinct, valid gameplay_phase span id within the same game trace as hubGameSpanID.
const hubGameplayPhaseSpanID = "00f067aa0ba902b7" // valid 8-byte hex span id

// gameplayPhaseCtx is gameTraceCtx additionally carrying the gameplay_phase span id.
func gameplayPhaseCtx(gameID string) gamecontext.GameContext {
	c := gameTraceCtx(gameID)
	c.GameplayPhaseSpanID = hubGameplayPhaseSpanID
	return c
}

// gameplayPhaseAsyncCtx is the async-path analog (activeContext + all three ids).
func gameplayPhaseAsyncCtx(gameID, serial string) gamecontext.GameContext {
	c := activeGameTraceContext(gameID, serial) // sets GameTraceID + GameTraceSpanID
	c.GameplayPhaseSpanID = hubGameplayPhaseSpanID
	return c
}

// TestSignalReceived_ChildOfGameplayPhaseSpan: the sync cycle-root agent.signal_received
// span (and thus the whole sync decision->action subtree) parents under the gameplay_phase
// span when its id is present — NOT the game root span.
func TestSignalReceived_ChildOfGameplayPhaseSpan(t *testing.T) {
	l, sr, _ := meteredTestLoop(t)
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
	l.Actions = &fakeSink{}

	l.OnEvaluate(context.Background(), gameplayPhaseCtx("game_gp01"),
		EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

	root := spansByName(sr.Ended(), SignalReceived)[0]
	wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
	wantSID, _ := trace.SpanIDFromHex(hubGameplayPhaseSpanID)
	rootSID, _ := trace.SpanIDFromHex(hubGameSpanID)

	if root.Parent().SpanID() != wantSID {
		t.Errorf("signal_received parent span_id = %s, want gameplay_phase %s", root.Parent().SpanID(), wantSID)
	}
	if root.Parent().SpanID() == rootSID {
		t.Errorf("signal_received parented under the GAME ROOT span %s; want gameplay_phase %s", rootSID, wantSID)
	}
	if root.Parent().TraceID() != wantTID {
		t.Errorf("signal_received parent trace_id = %s, want game trace %s", root.Parent().TraceID(), wantTID)
	}
	if !root.Parent().IsRemote() {
		t.Error("signal_received parent must be the REMOTE gameplay_phase span")
	}
	if root.SpanContext().TraceID() != wantTID {
		t.Errorf("signal_received must live in the game trace; got %s want %s", root.SpanContext().TraceID(), wantTID)
	}
}

// TestSignalReceived_FallsBackToGameRootWhenGameplayPhaseAbsent: with the gameplay_phase
// id empty but the game root id present, the span parents under the game ROOT (tier 2 —
// the #1187 behavior). This proves the fallback never loses the existing correlation.
func TestSignalReceived_FallsBackToGameRootWhenGameplayPhaseAbsent(t *testing.T) {
	l, sr, _ := meteredTestLoop(t)
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
	l.Actions = &fakeSink{}

	// gameTraceCtx has GameplayPhaseSpanID == "" (tier-2 input).
	l.OnEvaluate(context.Background(), gameTraceCtx("game_gp02"),
		EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

	root := spansByName(sr.Ended(), SignalReceived)[0]
	wantSID, _ := trace.SpanIDFromHex(hubGameSpanID)
	if root.Parent().SpanID() != wantSID {
		t.Errorf("signal_received parent span_id = %s, want game ROOT %s (fallback)", root.Parent().SpanID(), wantSID)
	}
	if !root.Parent().IsRemote() {
		t.Error("signal_received parent must be the REMOTE game root span on fallback")
	}
}

// TestSignalReceived_OwnRootWhenBothAbsent: tier 3 — both ids absent -> own-root span,
// the pre-#1187 shape.
func TestSignalReceived_OwnRootWhenBothAbsent(t *testing.T) {
	l, sr, _ := meteredTestLoop(t)
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
	l.Actions = &fakeSink{}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"},
		EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

	root := spansByName(sr.Ended(), SignalReceived)[0]
	if root.Parent().IsValid() {
		t.Errorf("signal_received must be own-root when no ids; parent=%v", root.Parent())
	}
}

// TestAsync_InferAndApplyChildrenOfGameplayPhaseSpan: the async infer.call + apply spans
// parent under the gameplay_phase span when present (not the game root), KEEPING the
// fire-cycle Link alongside.
func TestAsync_InferAndApplyChildrenOfGameplayPhaseSpan(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", gameplayPhaseAsyncCtx("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), gameplayPhaseAsyncCtx("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
	wantSID, _ := trace.SpanIDFromHex(hubGameplayPhaseSpanID)
	rootSID, _ := trace.SpanIDFromHex(hubGameSpanID)

	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		s := spans[0]
		if s.Parent().SpanID() != wantSID {
			t.Errorf("%s parent span_id = %s, want gameplay_phase %s", name, s.Parent().SpanID(), wantSID)
		}
		if s.Parent().SpanID() == rootSID {
			t.Errorf("%s parented under the GAME ROOT span; want gameplay_phase", name)
		}
		if s.Parent().TraceID() != wantTID {
			t.Errorf("%s parent trace_id = %s, want game trace %s", name, s.Parent().TraceID(), wantTID)
		}
		if !s.Parent().IsRemote() {
			t.Errorf("%s parent must be the REMOTE gameplay_phase span", name)
		}
		if _, ok := linkToFireCycle(s.Links()); !ok {
			t.Errorf("%s must keep its fire_cycle Link alongside the gameplay_phase parent", name)
		}
	}
}

// TestAsync_FallsBackToGameRootWhenGameplayPhaseAbsent: tier 2 for the async path — with
// only the game root id, the async spans parent under the game ROOT (#1187 behavior).
func TestAsync_FallsBackToGameRootWhenGameplayPhaseAbsent(t *testing.T) {
	be := &injectingBackend{name: "phi4-mini", response: validShieldResponse}
	provider := newFakeContextProvider()
	provider.set("g1", activeGameTraceContext("g1", "AAAA")) // no gameplay_phase id
	l, sr, _ := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeGameTraceContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()

	wantSID, _ := trace.SpanIDFromHex(hubGameSpanID)
	for _, name := range []string{SpanLLMInferCall, SpanLLMApply} {
		spans := spansByName(sr.Ended(), name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		if spans[0].Parent().SpanID() != wantSID {
			t.Errorf("%s parent span_id = %s, want game ROOT %s (fallback)", name, spans[0].Parent().SpanID(), wantSID)
		}
	}
}

// TestInGameRemoteParent_TwoTierFallback exercises the resolver directly: gameplay_phase
// wins when valid; game root is used when gameplay_phase is empty/invalid; own-root when
// both are empty/invalid.
func TestInGameRemoteParent_TwoTierFallback(t *testing.T) {
	parentSpanID := func(ctx context.Context) trace.SpanID {
		return trace.SpanContextFromContext(ctx).SpanID()
	}

	t.Run("gameplay_phase wins", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, hubGameplayPhaseSpanID)
		want, _ := trace.SpanIDFromHex(hubGameplayPhaseSpanID)
		if !ok || parentSpanID(ctx) != want {
			t.Errorf("want gameplay_phase parent %s (ok=%v), got %s", want, ok, parentSpanID(ctx))
		}
	})

	t.Run("falls back to game root when gameplay_phase empty", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, "")
		want, _ := trace.SpanIDFromHex(hubGameSpanID)
		if !ok || parentSpanID(ctx) != want {
			t.Errorf("want game-root parent %s (ok=%v), got %s", want, ok, parentSpanID(ctx))
		}
	})

	t.Run("falls back to game root when gameplay_phase invalid", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), hubGameTrace, hubGameSpanID, "not-hex")
		want, _ := trace.SpanIDFromHex(hubGameSpanID)
		if !ok || parentSpanID(ctx) != want {
			t.Errorf("want game-root parent %s (ok=%v), got %s", want, ok, parentSpanID(ctx))
		}
	})

	t.Run("own-root when both empty", func(t *testing.T) {
		base := context.Background()
		ctx, ok := inGameRemoteParent(base, hubGameTrace, "", "")
		if ok || trace.SpanContextFromContext(ctx).IsValid() {
			t.Errorf("want own-root (ok=false), got ok=%v parent=%v", ok, trace.SpanContextFromContext(ctx))
		}
	})

	t.Run("own-root when trace id empty", func(t *testing.T) {
		ctx, ok := inGameRemoteParent(context.Background(), "", hubGameSpanID, hubGameplayPhaseSpanID)
		if ok || trace.SpanContextFromContext(ctx).IsValid() {
			t.Errorf("want own-root when trace id empty (ok=false), got ok=%v", ok)
		}
	})
}
