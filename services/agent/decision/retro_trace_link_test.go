package decision

import (
	"testing"

	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// #1140 Slice B: the post-game agent.llm.retro span stays a ROOT span but is no
// longer orphaned from the game — when the finished session carries a valid game
// trace_id (GameContext.GameTraceID, #1133) the retro span carries an OTel Link to
// that game trace (the same gameTraceLink primitive the agent.decision span uses).
// Absent/invalid trace_id -> no Link, no error (graceful fallback). The link
// complements, does not replace, the game.id attribute.

const retroGameSpanID = "051581bf3cb55c13" // valid 8-byte hex span id (#1157)

// endedSessionWithTrace is a finished-game GameContext carrying a game trace_id
// (paired with a valid span_id, #1157, so a valid trace alone produces a link).
func endedSessionWithTrace(traceID string) gamecontext.GameContext {
	c := endedSession()
	c.GameTraceID = traceID
	c.GameTraceSpanID = retroGameSpanID
	return c
}

// TestRetroSpan_LinksToGameTrace: a valid GameTraceID yields exactly one Link on the
// agent.llm.retro span whose target trace_id is that game trace, the link carries
// game.trace_id as an attribute, and the span itself stamps game.trace_id as a
// queryable tag. The retro stays a root span (no parent).
func TestRetroSpan_LinksToGameTrace(t *testing.T) {
	const gameTrace = "4bf92f3577b34da6a3ce929d0e0e4736" // valid 16-byte hex trace id

	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSessionWithTrace(gameTrace))

	spans := spansByName(sr.Ended(), SpanLLMRetro)
	if len(spans) != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1", len(spans))
	}
	span := spans[0]

	// Root span: no parent (post-game / async).
	if span.Parent().IsValid() {
		t.Error("agent.llm.retro must remain a root span (no parent)")
	}

	links := span.Links()
	if len(links) != 1 {
		t.Fatalf("agent.llm.retro links = %d, want 1", len(links))
	}
	wantTID, _ := trace.TraceIDFromHex(gameTrace)
	if got := links[0].SpanContext.TraceID(); got != wantTID {
		t.Errorf("link trace_id = %s, want %s", got, wantTID)
	}
	if !links[0].SpanContext.TraceID().IsValid() {
		t.Error("link trace_id must be valid")
	}
	// #1157: the retro link now targets the actual game-start span.
	wantSID, _ := trace.SpanIDFromHex(retroGameSpanID)
	if got := links[0].SpanContext.SpanID(); got != wantSID {
		t.Errorf("link span_id = %s, want %s", got, wantSID)
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
		if string(kv.Key) == AttrGameTraceSpanID && kv.Value.AsString() == retroGameSpanID {
			foundSpanAttr = true
		}
	}
	if !foundTraceAttr {
		t.Errorf("link must carry %s=%s", AttrGameTraceID, gameTrace)
	}
	if !foundSpanAttr {
		t.Errorf("link must carry %s=%s", AttrGameTraceSpanID, retroGameSpanID)
	}
	// The trace id is ALSO a plain span attribute (searchable on backends that don't
	// surface link attributes).
	if v, ok := attrValue(span, AttrGameTraceID); !ok || v.AsString() != gameTrace {
		t.Errorf("span %s = %q (present=%v), want %q", AttrGameTraceID, v.AsString(), ok, gameTrace)
	}
}

// TestRetroSpan_NoLinkWhenTraceAbsent: a finished game with no GameTraceID (a shadow
// game, or any game before the correlation signal arrives) emits the retro span with
// NO Link and NO game.trace_id attribute — graceful fallback, byte-identical to
// before #1140. The game.id attribute is still present.
func TestRetroSpan_NoLinkWhenTraceAbsent(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession()) // no GameTraceID

	spans := spansByName(sr.Ended(), SpanLLMRetro)
	if len(spans) != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1", len(spans))
	}
	span := spans[0]
	if n := len(span.Links()); n != 0 {
		t.Errorf("agent.llm.retro links = %d, want 0 (no game trace_id)", n)
	}
	if _, ok := attrValue(span, AttrGameTraceID); ok {
		t.Error("game.trace_id attribute must be absent when no game trace_id is present")
	}
	if v, ok := attrValue(span, AttrGameID); !ok || v.AsString() == "" {
		t.Error("game.id attribute must survive even without a link")
	}
}

// TestRetroSpan_NoLinkWhenTraceInvalid: a malformed/garbage trace_id must NOT produce
// a Link and must NOT panic — the span is created as if no trace_id were present.
func TestRetroSpan_NoLinkWhenTraceInvalid(t *testing.T) {
	for _, bad := range []string{
		"not-hex",                          // unparseable
		"00000000000000000000000000000000", // all-zero (invalid trace id)
		"abc",                              // wrong length
	} {
		rc, sr := recordingRetro(t, retroFlagSnapshot())
		rc.OnGameEnd(endedSessionWithTrace(bad))

		spans := spansByName(sr.Ended(), SpanLLMRetro)
		if len(spans) != 1 {
			t.Fatalf("GameTraceID=%q: agent.llm.retro spans = %d, want 1", bad, len(spans))
		}
		if n := len(spans[0].Links()); n != 0 {
			t.Errorf("GameTraceID=%q: links = %d, want 0 (invalid id yields no link)", bad, n)
		}
	}
}
