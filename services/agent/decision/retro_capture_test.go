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
