package decision

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// hub_spoke_link_test.go covers the #1174 hub-and-spoke correlation additions: every
// agent per-game span carries the game.id query axis AND a Link back to the
// game-session trace (the hub). The intervention.effect span additionally Links to its
// CAUSING agent.decision span (link.kind=decision). All Links are nil-safe.

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

// linkByKind returns the Link on the span whose link.kind attribute equals kind, and
// whether one was found.
func linkByKind(links []sdktrace.Link, kind string) (sdktrace.Link, bool) {
	for _, lk := range links {
		for _, kv := range lk.Attributes {
			if string(kv.Key) == attrLinkKind && kv.Value.AsString() == kind {
				return lk, true
			}
		}
	}
	return sdktrace.Link{}, false
}

// rootEffectSpan returns the root agent.intervention.effect span (the one carrying the
// effect id but NO per-signal key), or nil.
func rootEffectSpan(spans []sdktrace.ReadOnlySpan) sdktrace.ReadOnlySpan {
	for _, s := range spans {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		if _, ok := attrValue(s, AttrEffectSignal); ok {
			continue // per-signal child
		}
		return s
	}
	return nil
}

// TestEffectSpan_LinksToGameTraceAndDecision: when both the game-trace ids AND a live
// decision span are in scope at schedule time, the emitted intervention.effect root
// carries (a) the game.id attribute, (b) a game-trace Link (to the hub), and (c) a
// decision Link (link.kind=decision) to the causing decision span. The per-signal
// child carries game.id + the intervention id.
func TestEffectSpan_LinksToGameTraceAndDecision(t *testing.T) {
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

	root := rootEffectSpan(sr.Ended())
	if root == nil {
		t.Fatal("no root agent.intervention.effect span")
	}

	// (a) game.id present.
	if v, ok := attrValue(root, AttrGameID); !ok || v.AsString() != gameID {
		t.Errorf("effect root game.id = %q (present=%v), want %q", v.AsString(), ok, gameID)
	}

	// (b) game-trace Link to the hub.
	wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
	wantSID, _ := trace.SpanIDFromHex(hubGameSpanID)
	var sawGameLink bool
	for _, lk := range root.Links() {
		if lk.SpanContext.TraceID() == wantTID && lk.SpanContext.SpanID() == wantSID {
			sawGameLink = true
		}
	}
	if !sawGameLink {
		t.Errorf("effect root missing game-trace Link to %s/%s; links=%v", wantTID, wantSID, root.Links())
	}
	// Searchable game.trace_id attribute too.
	if v, ok := attrValue(root, AttrGameTraceID); !ok || v.AsString() != hubGameTrace {
		t.Errorf("effect root game.trace_id = %q (present=%v), want %q", v.AsString(), ok, hubGameTrace)
	}

	// (c) decision Link (link.kind=decision) targeting the causing decision span.
	decLink, ok := linkByKind(root.Links(), linkKindDecision)
	if !ok {
		t.Fatal("effect root missing link.kind=decision Link to the causing decision span")
	}
	if decLink.SpanContext.TraceID() != decisionSpan.SpanContext().TraceID() {
		t.Errorf("decision Link trace_id = %s, want %s",
			decLink.SpanContext.TraceID(), decisionSpan.SpanContext().TraceID())
	}
	if decLink.SpanContext.SpanID() != decisionSpan.SpanContext().SpanID() {
		t.Errorf("decision Link span_id = %s, want %s",
			decLink.SpanContext.SpanID(), decisionSpan.SpanContext().SpanID())
	}

	// per-signal child carries game.id + the intervention id (both keys).
	var sawChild bool
	for _, s := range sr.Ended() {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		if _, ok := attrValue(s, AttrEffectSignal); !ok {
			continue
		}
		sawChild = true
		if v, ok := attrValue(s, AttrGameID); !ok || v.AsString() != gameID {
			t.Errorf("effect child game.id = %q (present=%v), want %q", v.AsString(), ok, gameID)
		}
		if v, ok := attrValue(s, AttrDecisionInterventionID); !ok || v.AsString() != id {
			t.Errorf("effect child %s = %q (present=%v), want %q", AttrDecisionInterventionID, v.AsString(), ok, id)
		}
	}
	if !sawChild {
		t.Fatal("no per-signal effect child span")
	}
}

// TestEffectSpan_NoLinksWhenIdsAbsent: with an empty GameContext and no live decision
// span, the effect span carries NO game-trace Link and NO decision Link (graceful
// nil-safe fallback) — exactly the pre-#1174 shape.
func TestEffectSpan_NoLinksWhenIdsAbsent(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_nolink"
	provider.set(gameID, durationContext(gameID, 90))

	l, sr, _, sched := effectLoop(t, provider, gameID)

	// background ctx => invalid SpanContext => no decision Link; empty GameContext => no
	// game-trace Link.
	id := l.scheduleEffectSample(context.Background(), gamecontext.GameContext{}, shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("expected a scheduled sample")
	}
	sched.fire()
	l.AwaitEffects()

	root := rootEffectSpan(sr.Ended())
	if root == nil {
		t.Fatal("no root agent.intervention.effect span")
	}
	if n := len(root.Links()); n != 0 {
		t.Errorf("effect root links = %d, want 0 (no ids => no Links); links=%v", n, root.Links())
	}
	if _, ok := attrValue(root, AttrGameTraceID); ok {
		t.Error("effect root carries game.trace_id attr despite empty ids")
	}
}

// TestSignalReceived_LinksToGameTrace: the cycle-root agent.signal_received span carries
// a game-trace Link + the searchable game.trace_id attr when the ids are present, and
// none when absent (nil-safe).
func TestSignalReceived_LinksToGameTrace(t *testing.T) {
	t.Run("present", func(t *testing.T) {
		l, sr, _ := meteredTestLoop(t)
		l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
		l.Actions = &fakeSink{}

		l.OnEvaluate(context.Background(), gameTraceCtx("game_sig01"),
			EvalTrigger{Signal: "metrics", T0: time.Now(), RPCService: "svc"})

		root := spansByName(sr.Ended(), SignalReceived)[0]
		wantTID, _ := trace.TraceIDFromHex(hubGameTrace)
		var sawLink bool
		for _, lk := range root.Links() {
			if lk.SpanContext.TraceID() == wantTID {
				sawLink = true
			}
		}
		if !sawLink {
			t.Errorf("signal_received missing game-trace Link; links=%v", root.Links())
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
		if n := len(root.Links()); n != 0 {
			t.Errorf("signal_received links = %d, want 0 (no ids); links=%v", n, root.Links())
		}
	})
}

// TestDecisionLink_Direct unit-tests the helper in isolation: a valid SpanContext yields
// one Link option carrying link.kind=decision; an invalid/zero SpanContext yields nil.
func TestDecisionLink_Direct(t *testing.T) {
	if got := decisionLink(trace.SpanContext{}); got != nil {
		t.Errorf("decisionLink(zero) = %v, want nil", got)
	}
	tid, _ := trace.TraceIDFromHex(hubGameTrace)
	sid, _ := trace.SpanIDFromHex(hubGameSpanID)
	sc := trace.NewSpanContext(trace.SpanContextConfig{TraceID: tid, SpanID: sid, TraceFlags: trace.FlagsSampled})
	if got := decisionLink(sc); len(got) != 1 {
		t.Errorf("decisionLink(valid) = %d options, want 1", len(got))
	}
}
