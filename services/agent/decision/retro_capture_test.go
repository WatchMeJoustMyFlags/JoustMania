package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// retroSnapshot is a representative llm-mode snapshot for the retro capture tests.
func retroFlagSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 0.7, "chaos": 0.3},
		Capability:           flags.Capability{Model: "phi4-mini"},
		InterventionsAllowed: []string{"noop", "grant_shield"},
	}
}

// recordingRetro builds a RetroCoordinator with a recording tracer and the given
// flag snapshot.
func recordingRetro(t *testing.T, snap flags.Snapshot) (*RetroCoordinator, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	rc := NewRetroCoordinator(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	rc.Tracer = tp.Tracer("test")
	return rc, sr
}

// endedSession is a finished-game GameContext: GameActive false, a session id, an
// elimination sequence, and a survivor.
func endedSession() gamecontext.GameContext {
	active := false
	dur := 90.0
	return gamecontext.GameContext{
		SessionID: "session-7",
		GameKind:  "real",
		Session: gamecontext.SessionSignals{
			GameActive:          &active,
			DurationSeconds:     &dur,
			EliminationSequence: []string{"BB:22"},
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"AA:11": {Serial: "AA:11"},
			"BB:22": {Serial: "BB:22"},
		},
	}
}

// TestRetroCapture_EmitsSpan: a normal game end emits exactly one agent.llm.retro
// span with non-empty prompt text.
func TestRetroCapture_EmitsSpan(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession())

	spans := spansByName(sr.Ended(), SpanLLMRetro)
	if len(spans) != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1", len(spans))
	}
	if v, ok := attrValue(spans[0], AttrLLMRetroSystem); !ok || v.AsString() == "" {
		t.Error("llm.retro.system must be present and non-empty")
	}
	if v, ok := attrValue(spans[0], AttrLLMRetroUser); !ok || v.AsString() == "" {
		t.Error("llm.retro.user must be present and non-empty")
	}
}

// TestRetroCapture_SchemaComplete: every attribute of the agent.llm.retro schema
// is present, with inference.used == "none" (the documented divergence — no rules
// fallback for a retrospective).
func TestRetroCapture_SchemaComplete(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.OnGameEnd(endedSession())

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	wantStr := map[string]string{
		"gen_ai.operation.name":  "chat",
		"gen_ai.request.model":   "phi4-mini",
		"gen_ai.output.type":     "json",
		AttrMode:                 "retro",
		AttrGameKind:             "real",
		AttrObjectives:           "chaos=0.3,endurance=0.7",
		AttrInterventionsAllowed: "noop,grant_shield",
		"session.id":             "session-7",
		AttrInferenceConfigured:  "phi4-mini",
		AttrInferenceUsed:        DefaultInference, // "none", NOT "rules"
		AttrInferenceFallback:    FallbackNoBackend,
	}
	for key, expected := range wantStr {
		if v, ok := attrValue(span, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
	// Explicit divergence guard: never "rules" on a retro span.
	if v, _ := attrValue(span, AttrInferenceUsed); v.AsString() == InferenceRules {
		t.Errorf("inference.used = %q, must NOT be %q for a retrospective", v.AsString(), InferenceRules)
	}
	// llm.retro.bytes equals the combined length of the two prompt texts.
	sys, _ := attrValue(span, AttrLLMRetroSystem)
	usr, _ := attrValue(span, AttrLLMRetroUser)
	wantBytes := int64(len(sys.AsString()) + len(usr.AsString()))
	if v, ok := attrValue(span, AttrLLMRetroBytes); !ok || v.AsInt64() != wantBytes {
		t.Errorf("llm.retro.bytes = %d, want %d", v.AsInt64(), wantBytes)
	}
}

// TestRetroCapture_ExactlyOncePerSession: the same SessionID twice emits ONE
// span; a new SessionID emits a second.
func TestRetroCapture_ExactlyOncePerSession(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	rc.OnGameEnd(endedSession())
	rc.OnGameEnd(endedSession()) // same session-7: deduped
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 1 {
		t.Fatalf("agent.llm.retro spans = %d, want 1 (same session deduped)", n)
	}

	next := endedSession()
	next.SessionID = "session-8"
	rc.OnGameEnd(next)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("agent.llm.retro spans = %d, want 2 (new session)", n)
	}
}

// TestRetroCapture_InterleavedGameEndDedupe: with concurrent games (#845 PR B),
// game-ends interleave (A ends, B ends, A's id AGAIN). The bounded LRU dedupe must
// suppress the repeated A while still having captured both A and B exactly once —
// the old single last-session string would have re-captured A here.
func TestRetroCapture_InterleavedGameEndDedupe(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	end := func(id string) {
		c := endedSession()
		c.SessionID = id
		rc.OnGameEnd(c)
	}

	end("game-A")
	end("game-B")
	end("game-A") // repeat of an already-captured session: must be suppressed

	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("agent.llm.retro spans = %d, want 2 (A,B once each; repeated A deduped)", n)
	}
}

// TestRetroCapture_DedupeWindowBounded: once more than retroDedupeWindow distinct
// sessions have been captured, the oldest falls out of the LRU and a repeat of it
// captures again — this bounds dedupe memory and is acceptable (a session id is
// never legitimately re-ended after that many newer games).
func TestRetroCapture_DedupeWindowBounded(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())

	end := func(id string) {
		c := endedSession()
		c.SessionID = id
		rc.OnGameEnd(c)
	}

	end("game-old")
	// Fill the window with retroDedupeWindow fresh sessions, evicting "game-old".
	for i := 0; i < retroDedupeWindow; i++ {
		end("fill-" + string(rune('a'+i)))
	}
	// "game-old" has aged out of the window, so it captures a second time.
	end("game-old")

	want := retroDedupeWindow + 2 // game-old (x2) + the window-fill sessions
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != want {
		t.Fatalf("agent.llm.retro spans = %d, want %d", n, want)
	}
}

// TestRetroCapture_SkipsActiveGame: a snapshot still reporting GameActive=true
// emits nothing (defensive — only end-of-game captures).
func TestRetroCapture_SkipsActiveGame(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	c := endedSession()
	active := true
	c.Session.GameActive = &active
	rc.OnGameEnd(c)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("agent.llm.retro spans = %d, want 0 for an active game", n)
	}
}

// TestRetroCapture_SkipsEmptySession: an empty SessionID emits nothing.
func TestRetroCapture_SkipsEmptySession(t *testing.T) {
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	c := endedSession()
	c.SessionID = ""
	rc.OnGameEnd(c)
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("agent.llm.retro spans = %d, want 0 for an empty session id", n)
	}
}
