package decision

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// hub_spoke_link_test.go covers the #1178 in-game re-parenting (refining #1174): the
// agent's per-game IN-GAME spans become remote CHILDREN of the game span instead of
// Links to the game trace. agent.signal_received re-parents under the game span (pulling
// its whole sync subtree + the async fire anchor into the game trace); the
// agent.intervention.effect span re-parents under its CAUSING agent.decision span. Every
// span keeps the game.id query axis. All re-parenting is graceful: absent/invalid ids ->
// own-root span exactly as before.

const (
	hubGameTrace  = "4bf92f3577b34da6a3ce929d0e0e4736" // valid 16-byte hex trace id
	hubGameSpanID = "051581bf3cb55c13"                 // valid 8-byte hex span id
)

// gameTraceCtx is a GameContext carrying the populated game-session trace ids (#1133).
func gameTraceCtx(gameID string) gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID:       gameID,
		GameKind:        "real",
		GameTraceID:     hubGameTrace,
		GameTraceSpanID: hubGameSpanID,
	}
}

// effectSpans returns every ended agent.intervention.effect span, failing the test
// when there is none.
func effectSpans(t *testing.T, spans []sdktrace.ReadOnlySpan) []sdktrace.ReadOnlySpan {
	t.Helper()
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == SpanInterventionEffect {
			out = append(out, s)
		}
	}
	if len(out) == 0 {
		t.Fatal("no agent.intervention.effect span")
	}
	return out
}

// TestEffectSpan_ChildOfDecision: when a live decision span is in scope at schedule time,
// every emitted intervention.effect span (one per signal, #1325) is a remote CHILD of that
// decision span — its Parent is the decision SpanContext and it lives in the decision's
// trace — and carries NO standalone Links (the decision is the parent). game.id and the
// intervention id stay the query axis; the game trace ids stay as searchable attributes.
func TestEffectSpan_ChildOfDecision(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_hub01"
	provider.set(gameID, durationContext(gameID, 90))

	l, sr, _, sched := effectLoop(t, provider, gameID)

	// Open a real decision span so its SpanContext is captured at schedule time.
	decisionCtx, decisionSpan := l.Tracer.Start(context.Background(), SpanDecision)

	id := l.scheduleEffectSample(decisionCtx, gameTraceCtx(gameID), shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("expected a scheduled sample")
	}
	decisionSpan.End()
	sched.fire()
	l.AwaitEffects()

	for _, span := range effectSpans(t, sr.Ended()) {
		if v, ok := attrValue(span, AttrGameID); !ok || v.AsString() != gameID {
			t.Errorf("effect game.id = %q (present=%v), want %q", v.AsString(), ok, gameID)
		}
		if v, ok := attrValue(span, AttrDecisionInterventionID); !ok || v.AsString() != id {
			t.Errorf("effect %s = %q (present=%v), want %q", AttrDecisionInterventionID, v.AsString(), ok, id)
		}
		if span.Parent().SpanID() != decisionSpan.SpanContext().SpanID() {
			t.Errorf("effect parent span_id = %s, want decision span %s",
				span.Parent().SpanID(), decisionSpan.SpanContext().SpanID())
		}
		if span.SpanContext().TraceID() != decisionSpan.SpanContext().TraceID() {
			t.Errorf("effect must share the decision trace; got %s want %s",
				span.SpanContext().TraceID(), decisionSpan.SpanContext().TraceID())
		}
		if n := len(span.Links()); n != 0 {
			t.Errorf("effect links = %d, want 0 (re-parented, not Linked); links=%v", n, span.Links())
		}
		if v, ok := attrValue(span, AttrGameTraceID); !ok || v.AsString() != hubGameTrace {
			t.Errorf("effect game.trace_id = %q (present=%v), want %q", v.AsString(), ok, hubGameTrace)
		}
	}
}

// TestEffectSpan_OwnRootWhenDecisionAbsent: with no live decision span in scope (and an
// empty GameContext) every effect span is its own root — no parent, no Links — exactly the
// graceful fallback the nil-safe Link produced before.
func TestEffectSpan_OwnRootWhenDecisionAbsent(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_nolink"
	provider.set(gameID, durationContext(gameID, 90))

	l, sr, _, sched := effectLoop(t, provider, gameID)

	// background ctx => invalid decision SpanContext => no remote parent; empty
	// GameContext => no game trace ids.
	id := l.scheduleEffectSample(context.Background(), gamecontext.GameContext{}, shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("expected a scheduled sample")
	}
	sched.fire()
	l.AwaitEffects()

	for _, span := range effectSpans(t, sr.Ended()) {
		if span.Parent().IsValid() {
			t.Errorf("effect must be own-root when no decision span in scope; parent=%v", span.Parent())
		}
		if n := len(span.Links()); n != 0 {
			t.Errorf("effect links = %d, want 0 (no ids => no Links); links=%v", n, span.Links())
		}
		if _, ok := attrValue(span, AttrGameTraceID); ok {
			t.Error("effect carries game.trace_id attr despite empty ids")
		}
	}
}

// TestSignalReceived_ChildOfGameSpan: the cycle-root agent.signal_received span is a
// remote CHILD of the game span when the game trace ids are present (its Parent is the
// game span, it lives in the game trace), and an OWN-ROOT span (no parent) when they are
// absent. The searchable game.trace_id attr rides along when present. The whole sync
// decision->action subtree is therefore pulled into the game trace.
func TestSignalReceived_ChildOfGameSpan(t *testing.T) {
	t.Run("present", func(t *testing.T) {
		l, sr, _ := meteredTestLoop(t)
		l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
		l.Actions = &fakeSink{}

		l.OnEvaluate(context.Background(), gameTraceCtx("game_sig01"),
			EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

		root := spansByName(sr.Ended(), SignalReceived)[0]
		wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
		wantSID, _ := trace.SpanIDFromHex(hubGameSpanID)
		if root.Parent().TraceID() != wantTID {
			t.Errorf("signal_received parent trace_id = %s, want game trace %s", root.Parent().TraceID(), wantTID)
		}
		if root.Parent().SpanID() != wantSID {
			t.Errorf("signal_received parent span_id = %s, want game span %s", root.Parent().SpanID(), wantSID)
		}
		if root.SpanContext().TraceID() != wantTID {
			t.Errorf("signal_received must live in the game trace; got %s want %s", root.SpanContext().TraceID(), wantTID)
		}
		if !root.Parent().IsRemote() {
			t.Error("signal_received parent must be the REMOTE game span")
		}
		// The decision span (sync subtree) shares the game trace as a consequence.
		dec := spansByName(sr.Ended(), SpanDecision)[0]
		if dec.SpanContext().TraceID() != wantTID {
			t.Errorf("agent.decision must be pulled into the game trace; got %s want %s", dec.SpanContext().TraceID(), wantTID)
		}
		if v, ok := attrValue(root, AttrGameTraceID); !ok || v.AsString() != hubGameTrace {
			t.Errorf("signal_received game.trace_id = %q (present=%v), want %q", v.AsString(), ok, hubGameTrace)
		}
	})

	t.Run("absent", func(t *testing.T) {
		l, sr, _ := meteredTestLoop(t)
		l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
		l.Actions = &fakeSink{}

		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"},
			EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

		root := spansByName(sr.Ended(), SignalReceived)[0]
		if root.Parent().IsValid() {
			t.Errorf("signal_received must be own-root when no game ids; parent=%v", root.Parent())
		}
		if n := len(root.Links()); n != 0 {
			t.Errorf("signal_received links = %d, want 0 (no ids); links=%v", n, root.Links())
		}
	})
}

// TestGameRemoteParent_Direct unit-tests the helper in isolation: a valid (trace,span)
// pair installs a remote parent (ok=true, a span started from the ctx becomes a child);
// any empty/invalid id returns the unchanged ctx + ok=false (graceful fallback, own root).
func TestGameRemoteParent_Direct(t *testing.T) {
	base := context.Background()

	for _, tc := range []struct {
		name            string
		traceID, spanID string
		wantOK          bool
	}{
		{"valid", hubGameTrace, hubGameSpanID, true},
		{"empty trace", "", hubGameSpanID, false},
		{"empty span", hubGameTrace, "", false},
		{"invalid trace", "zzzz", hubGameSpanID, false},
		{"invalid span", hubGameTrace, "zz", false},
		{"all-zero trace", "00000000000000000000000000000000", hubGameSpanID, false},
		{"all-zero span", hubGameTrace, "0000000000000000", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, ok := gameRemoteParent(base, tc.traceID, tc.spanID)
			if ok != tc.wantOK {
				t.Fatalf("gameRemoteParent ok = %v, want %v", ok, tc.wantOK)
			}
			if !tc.wantOK {
				if ctx != base {
					t.Error("invalid ids must return the unchanged ctx")
				}
				return
			}
			// ok=true: a span started from ctx is a remote child of the game span.
			wantTID, _ := trace.TraceIDFromHex(tc.traceID)
			wantSID, _ := trace.SpanIDFromHex(tc.spanID)
			sc := trace.SpanContextFromContext(ctx)
			if sc.TraceID() != wantTID || sc.SpanID() != wantSID {
				t.Errorf("remote parent = %s/%s, want %s/%s", sc.TraceID(), sc.SpanID(), wantTID, wantSID)
			}
			if !sc.IsRemote() {
				t.Error("remote parent SpanContext must be marked Remote")
			}
		})
	}
}
