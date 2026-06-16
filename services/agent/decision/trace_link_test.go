package decision

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// trace-correlation Phase 2 (#1133): when the coordinator's game-span trace_id is
// known (ingested into GameContext.GameTraceID), the agent.decision span carries an
// OTel Link to that trace; absent/invalid -> no Link, no error. The #1095 game.id
// attribute stays in every case (Links complement, do not replace, the attribute).

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
		SessionID:   gameID,
		GameKind:    "real",
		GameTraceID: gameTrace,
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
	// The link target must validate so Jaeger renders it as navigable.
	if !links[0].SpanContext.TraceID().IsValid() {
		t.Error("link trace_id must be valid")
	}
	// The trace id is also stamped on the link as a queryable attribute.
	var foundAttr bool
	for _, kv := range links[0].Attributes {
		if string(kv.Key) == AttrGameTraceID && kv.Value.AsString() == gameTrace {
			foundAttr = true
		}
	}
	if !foundAttr {
		t.Errorf("link must carry %s=%s", AttrGameTraceID, gameTrace)
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
	for _, bad := range []string{
		"not-hex",                          // unparseable
		"00000000000000000000000000000000", // all-zero (invalid trace id)
		"abc",                              // wrong length
	} {
		n, _, _, _ := decisionSpan(t, gamecontext.GameContext{SessionID: "g", GameTraceID: bad})
		if n != 0 {
			t.Errorf("GameTraceID=%q: links = %d, want 0 (invalid id yields no link)", bad, n)
		}
	}
}

// TestGameTraceLink_Direct unit-tests the helper in isolation: valid -> one link
// option, empty/invalid -> nil so the span is started unchanged.
func TestGameTraceLink_Direct(t *testing.T) {
	if got := gameTraceLink(""); got != nil {
		t.Errorf("gameTraceLink(empty) = %v, want nil", got)
	}
	if got := gameTraceLink("zzzz"); got != nil {
		t.Errorf("gameTraceLink(invalid) = %v, want nil", got)
	}
	if got := gameTraceLink("4bf92f3577b34da6a3ce929d0e0e4736"); len(got) != 1 {
		t.Errorf("gameTraceLink(valid) = %d options, want 1", len(got))
	}
}
