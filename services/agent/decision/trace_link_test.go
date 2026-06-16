package decision

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// trace-correlation Phase 2 (#1133/#1157): when the coordinator's game-span
// trace_id AND span_id are known (ingested into GameContext.GameTraceID /
// GameTraceSpanID), the agent.decision span carries an OTel Link to that game's
// root span; either id absent/invalid -> no Link, no error. The #1095 game.id
// attribute stays in every case (Links complement, do not replace, the attribute).

const linkGameSpanID = "051581bf3cb55c13" // valid 8-byte hex span id

func linkSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 10},
	}
}

func decisionSpan(t *testing.T, ctx gamecontext.GameContext) (linkCount int, linkedTrace string, gameIDAttr string, present bool) {
	t.Helper()
	l, sr := recordingLoop(t, linkSnapshot(), []Decision{{Intervention: "grant_shield", Reason: "x"}})
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	dec := spansByName(sr.Ended(), SpanDecision)[0]
	links := dec.Links()
	if v, ok := attrValue(dec, AttrGameID); ok {
		gameIDAttr, present = v.AsString(), true
	}
	if len(links) == 1 {
		return 1, links[0].SpanContext.TraceID().String(), gameIDAttr, present
	}
	return len(links), "", gameIDAttr, present
}

// TestDecisionSpan_LinksToGameTrace: a valid GameTraceID yields exactly one Link on
// the agent.decision span whose target trace_id is that game trace, AND the link
// carries game.trace_id as an attribute. The #1095 game.id attribute is still present.
func TestDecisionSpan_LinksToGameTrace(t *testing.T) {
	const gameTrace = "4bf92f3577b34da6a3ce929d0e0e4736" // valid 16-byte hex trace id
	const gameID = "game_abc123def456"

	l, sr := recordingLoop(t, linkSnapshot(), []Decision{{Intervention: "grant_shield", Reason: "x"}})
	l.OnEvaluate(context.Background(), gamecontext.GameContext{
		SessionID:       gameID,
		GameKind:        "real",
		GameTraceID:     gameTrace,
		GameTraceSpanID: linkGameSpanID,
	}, testTrigger())

	dec := spansByName(sr.Ended(), SpanDecision)[0]
	links := dec.Links()
	if len(links) != 1 {
		t.Fatalf("agent.decision links = %d, want 1", len(links))
	}
	wantTID, _ := trace.TraceIDFromHex(gameTrace)
	if got := links[0].SpanContext.TraceID(); got != wantTID {
		t.Errorf("link trace_id = %s, want %s", got, wantTID)
	}
	// #1157: the link now targets the actual game-start span, not just the trace.
	wantSID, _ := trace.SpanIDFromHex(linkGameSpanID)
	if got := links[0].SpanContext.SpanID(); got != wantSID {
		t.Errorf("link span_id = %s, want %s", got, wantSID)
	}
	// The link target must validate so Jaeger renders it as navigable.
	if !links[0].SpanContext.TraceID().IsValid() {
		t.Error("link trace_id must be valid")
	}
	if !links[0].SpanContext.SpanID().IsValid() {
		t.Error("link span_id must be valid")
	}
	// Both ids are stamped on the link as queryable attributes.
	var foundTraceAttr, foundSpanAttr bool
	for _, kv := range links[0].Attributes {
		if string(kv.Key) == AttrGameTraceID && kv.Value.AsString() == gameTrace {
			foundTraceAttr = true
		}
		if string(kv.Key) == AttrGameTraceSpanID && kv.Value.AsString() == linkGameSpanID {
			foundSpanAttr = true
		}
	}
	if !foundTraceAttr {
		t.Errorf("link must carry %s=%s", AttrGameTraceID, gameTrace)
	}
	if !foundSpanAttr {
		t.Errorf("link must carry %s=%s", AttrGameTraceSpanID, linkGameSpanID)
	}
	// Phase 1 (#1095) game.id attribute is untouched by the Link.
	if v, ok := attrValue(dec, AttrGameID); !ok || v.AsString() != gameID {
		t.Errorf("game.id = %q (present=%v), want %q", v.AsString(), ok, gameID)
	}
}

// TestDecisionSpan_NoLinkWhenTraceAbsent: shadow games (or any game before the
// correlation signal arrives) have no GameTraceID — the span is created with NO Link
// and NO error, and the game.id attribute is still present (graceful fallback).
func TestDecisionSpan_NoLinkWhenTraceAbsent(t *testing.T) {
	n, _, gameID, present := decisionSpan(t, gamecontext.GameContext{SessionID: "game_noTrace", GameKind: "shadow"})
	if n != 0 {
		t.Fatalf("agent.decision links = %d, want 0 (no game trace_id)", n)
	}
	if !present || gameID != "game_noTrace" {
		t.Errorf("game.id = %q (present=%v), want %q present — attribute must survive even without a link", gameID, present, "game_noTrace")
	}
}

// TestDecisionSpan_NoLinkWhenTraceInvalid: a malformed/garbage trace_id (e.g. an
// unparseable or all-zero id) must NOT produce a Link and must NOT panic — the span is
// created exactly as if no trace_id were present.
func TestDecisionSpan_NoLinkWhenTraceInvalid(t *testing.T) {
	const validTrace = "4bf92f3577b34da6a3ce929d0e0e4736"
	for _, bad := range []string{
		"not-hex",                          // unparseable
		"00000000000000000000000000000000", // all-zero (invalid trace id)
		"abc",                              // wrong length
	} {
		// Pair the bad trace_id with a valid span_id so the trace_id is solely
		// what suppresses the link.
		n, _, _, _ := decisionSpan(t, gamecontext.GameContext{SessionID: "g", GameTraceID: bad, GameTraceSpanID: linkGameSpanID})
		if n != 0 {
			t.Errorf("GameTraceID=%q: links = %d, want 0 (invalid id yields no link)", bad, n)
		}
	}
}

// TestDecisionSpan_NoLinkWhenSpanIDMissingOrInvalid (#1157): a valid game trace_id
// is no longer sufficient on its own — without a valid span_id the link target
// would carry an all-zero span_id, so the loop emits NO link (graceful fallback).
func TestDecisionSpan_NoLinkWhenSpanIDMissingOrInvalid(t *testing.T) {
	const validTrace = "4bf92f3577b34da6a3ce929d0e0e4736"
	for _, badSpan := range []string{
		"",                 // pre-#1157 emitter / unsampled span
		"zz",               // unparseable
		"0000000000000000", // all-zero (invalid span id)
		"abc",              // wrong length
	} {
		n, _, _, _ := decisionSpan(t, gamecontext.GameContext{SessionID: "g", GameTraceID: validTrace, GameTraceSpanID: badSpan})
		if n != 0 {
			t.Errorf("GameTraceSpanID=%q: links = %d, want 0 (missing/invalid span_id yields no link)", badSpan, n)
		}
	}
}

// TestGameTraceLink_Direct unit-tests the helper in isolation: a valid (trace,span)
// pair -> one link option; any empty/invalid id -> nil so the span is started unchanged.
func TestGameTraceLink_Direct(t *testing.T) {
	const validTrace = "4bf92f3577b34da6a3ce929d0e0e4736"
	if got := gameTraceLink("", linkGameSpanID); got != nil {
		t.Errorf("gameTraceLink(empty trace) = %v, want nil", got)
	}
	if got := gameTraceLink(validTrace, ""); got != nil {
		t.Errorf("gameTraceLink(empty span) = %v, want nil", got)
	}
	if got := gameTraceLink("zzzz", linkGameSpanID); got != nil {
		t.Errorf("gameTraceLink(invalid trace) = %v, want nil", got)
	}
	if got := gameTraceLink(validTrace, "zz"); got != nil {
		t.Errorf("gameTraceLink(invalid span) = %v, want nil", got)
	}
	if got := gameTraceLink(validTrace, linkGameSpanID); len(got) != 1 {
		t.Errorf("gameTraceLink(valid) = %d options, want 1", len(got))
	}
}
