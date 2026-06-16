package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/llm"
)

// retro_inference_test.go covers the #1179 wiring: the retro now actually CALLS the
// resolved backend at game end (async), decodes the conclusion, and stamps it on the
// agent.llm.retro span. The tests assert the success conclusion + per-suggestion
// events, the parse-failure path (parse_ok=false + error), the no-block guarantee of
// the lifecycle hook, and the capture-only fallback when no backend resolves.

// reachableRetroResolver builds a Resolver whose configured inference tier is reachable
// (a real loopback listener) and whose Infer delegate returns the given canned reply.
func reachableRetroResolver(t *testing.T, model string, infer func(context.Context, llm.Prompt) (string, error)) *Resolver {
	t.Helper()
	addr, closeFn := listenLocal(t)
	t.Cleanup(closeFn)
	r := NewInferenceResolver(InferenceTier{Model: model, Addr: addr, Infer: infer}, Endpoints{}, 0)
	r.Refresh(context.Background())
	return r
}

// eventByName returns the first span event with the given name, or false.
func eventsByName(span sdktrace.ReadOnlySpan, name string) []sdktrace.Event {
	var out []sdktrace.Event
	for _, ev := range span.Events() {
		if ev.Name == name {
			out = append(out, ev)
		}
	}
	return out
}

func eventAttr(ev sdktrace.Event, key string) (string, bool) {
	for _, kv := range ev.Attributes {
		if string(kv.Key) == key {
			return kv.Value.AsString(), true
		}
	}
	return "", false
}

// TestRetro_CapturesConclusionOnSuccess: with a reachable backend that returns a valid
// analyst reply, the retro span carries the decoded assessment / focus / suggestion
// count, one retro.suggestion event per suggestion, parse_ok=true, a latency, and the
// raw-response fingerprint.
func TestRetro_CapturesConclusionOnSuccess(t *testing.T) {
	resetRetroResponseRef()
	reply := `{
	  "session_assessment": "close finish",
	  "suggestions": [
	    {"intervention_type": "grant_shield", "emphasis": "weight_up", "reason": "fast early dropouts"}
	  ],
	  "session_focus": "endurance"
	}`
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return reply, nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrLLMRetroSessionAssessment); !ok || v.AsString() != "close finish" {
		t.Errorf("session_assessment = %q (present=%v)", v.AsString(), ok)
	}
	if v, ok := attrValue(span, AttrLLMRetroSessionFocus); !ok || v.AsString() != "endurance" {
		t.Errorf("session_focus = %q (present=%v)", v.AsString(), ok)
	}
	if v, ok := attrValue(span, AttrLLMRetroSuggestionCount); !ok || v.AsInt64() != 1 {
		t.Errorf("suggestion_count = %d (present=%v), want 1", v.AsInt64(), ok)
	}
	if v, ok := attrValue(span, AttrLLMRetroParseOK); !ok || !v.AsBool() {
		t.Errorf("parse_ok = %v (present=%v), want true", v.AsBool(), ok)
	}
	if _, ok := attrValue(span, AttrLLMRetroLatencyMs); !ok {
		t.Error("llm.retro.latency_ms must be present")
	}
	if v, ok := attrValue(span, AttrLLMRetroResponseSHA); !ok || v.AsString() == "" {
		t.Error("llm.retro.response_sha256 must be present and non-empty")
	}
	// One queryable event per suggestion, carrying the decoded fields.
	evs := eventsByName(span, SpanLLMRetroSuggestion)
	if len(evs) != 1 {
		t.Fatalf("retro.suggestion events = %d, want 1", len(evs))
	}
	if v, _ := eventAttr(evs[0], AttrRetroSuggestionType); v != "grant_shield" {
		t.Errorf("event intervention_type = %q, want grant_shield", v)
	}
	if v, _ := eventAttr(evs[0], AttrRetroSuggestionEmphasis); v != "weight_up" {
		t.Errorf("event emphasis = %q, want weight_up", v)
	}
	if v, _ := eventAttr(evs[0], AttrRetroSuggestionReason); v != "fast early dropouts" {
		t.Errorf("event reason = %q", v)
	}
	// No error on the success path.
	if _, ok := attrValue(span, AttrLLMInferError); ok {
		t.Error("llm.infer.error must be ABSENT on a parsed reply")
	}
}

// TestRetro_EmitsRawResponseOncePerHash: the full raw reply rides a once-per-fingerprint
// reference log, not the span. We assert via the reference singleton's emit returning
// false (already-emitted) for the same raw text after the retro ran.
func TestRetro_EmitsRawResponseOncePerHash(t *testing.T) {
	resetRetroResponseRef()
	reply := `{"session_assessment":"ok","suggestions":[],"session_focus":"balanced"}`
	rc, _ := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return reply, nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	// The retro already emitted the raw reply once; a second emit of the SAME text is a
	// no-op (returns false), proving the once-per-hash behavior.
	if retroResponseRef.emit(slog.New(slog.NewTextHandler(io.Discard, nil)), responseSHA(reply), "session-7", reply) {
		t.Error("raw response should already be emitted once (once-per-hash), got a fresh emit")
	}
	// A DIFFERENT raw text emits fresh.
	if !retroResponseRef.emit(slog.New(slog.NewTextHandler(io.Discard, nil)), responseSHA("other"), "s", "other") {
		t.Error("a distinct raw response should emit fresh")
	}
}

// TestRetro_ParseFailureStampsError: a reachable backend that returns garbage gets
// parse_ok=false + llm.infer.error and NO conclusion attributes (fail-open: the span
// still ends cleanly).
func TestRetro_ParseFailureStampsError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return "not json at all", nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrLLMRetroParseOK); !ok || v.AsBool() {
		t.Errorf("parse_ok = %v (present=%v), want false", v.AsBool(), ok)
	}
	if v, ok := attrValue(span, AttrLLMInferError); !ok || v.AsString() == "" {
		t.Error("llm.infer.error must be present on an unparseable reply")
	}
	if _, ok := attrValue(span, AttrLLMRetroSessionAssessment); ok {
		t.Error("session_assessment must be ABSENT on a parse failure")
	}
	// Latency is still recorded even on failure.
	if _, ok := attrValue(span, AttrLLMRetroLatencyMs); !ok {
		t.Error("llm.retro.latency_ms must be present even on a parse failure")
	}
}

// TestRetro_TransportErrorStampsError: a reachable backend whose Infer ERRORS gets
// parse_ok=false + llm.infer.error carrying the transport error, no conclusion, span
// still ends (fail-open).
func TestRetro_TransportErrorStampsError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return "", errors.New("boom") }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if v, ok := attrValue(span, AttrLLMRetroParseOK); !ok || v.AsBool() {
		t.Errorf("parse_ok = %v (present=%v), want false", v.AsBool(), ok)
	}
	if v, ok := attrValue(span, AttrLLMInferError); !ok || v.AsString() != "boom" {
		t.Errorf("llm.infer.error = %q (present=%v), want \"boom\"", v.AsString(), ok)
	}
	// No raw-response fingerprint on a transport error (there is no reply text).
	if _, ok := attrValue(span, AttrLLMRetroResponseSHA); ok {
		t.Error("response_sha256 must be ABSENT on a transport error (no reply)")
	}
}

// TestRetro_OnGameEndDoesNotBlock: OnGameEnd is a lifecycle hook — it must return
// promptly even when Infer is slow. We block the delegate on a channel and assert
// OnGameEnd returns before the call completes, then release it.
func TestRetro_OnGameEndDoesNotBlock(t *testing.T) {
	resetRetroResponseRef()
	release := make(chan struct{})
	var calledOnce sync.Once
	started := make(chan struct{})
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) {
			calledOnce.Do(func() { close(started) })
			<-release // block until the test releases us
			return `{"session_assessment":"ok","suggestions":[],"session_focus":"balanced"}`, nil
		}))

	done := make(chan struct{})
	go func() { rc.OnGameEnd(endedSession()); close(done) }()

	select {
	case <-done:
		// OnGameEnd returned without waiting on the (still-blocked) Infer — the goal.
	case <-time.After(2 * time.Second):
		close(release)
		t.Fatal("OnGameEnd did not return promptly; it blocked on the inference call")
	}

	// The inference IS in flight (the delegate started) but the span has not ended yet.
	<-started
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 0 {
		t.Errorf("retro span ended before inference completed: got %d ended spans", n)
	}

	close(release) // let the goroutine finish
	rc.AwaitInflight()
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 1 {
		t.Errorf("after inference, retro spans = %d, want 1", n)
	}
}

// TestRetro_CaptureOnlyWhenNoBackend: with no resolver wired (nil backend), the retro
// stays capture-only — the span ends synchronously, carries no conclusion, no
// parse_ok, and reports no_backend_available.
func TestRetro_CaptureOnlyWhenNoBackend(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot()) // no SetResolver -> nil backend

	rc.OnGameEnd(endedSession())
	// No AwaitInflight needed: with no backend the span ends synchronously in OnGameEnd.

	spans := spansByName(sr.Ended(), SpanLLMRetro)
	if len(spans) != 1 {
		t.Fatalf("retro spans = %d, want 1", len(spans))
	}
	span := spans[0]
	if v, ok := attrValue(span, AttrInferenceFallback); !ok || v.AsString() != FallbackNoBackend {
		t.Errorf("fallback_reason = %q (present=%v), want %q", v.AsString(), ok, FallbackNoBackend)
	}
	// No conclusion was captured (no backend was called).
	for _, key := range []string{AttrLLMRetroParseOK, AttrLLMRetroSessionAssessment, AttrLLMRetroLatencyMs, AttrLLMRetroResponseSHA} {
		if _, ok := attrValue(span, key); ok {
			t.Errorf("attr %s must be ABSENT on the capture-only path", key)
		}
	}
	if n := len(eventsByName(span, SpanLLMRetroSuggestion)); n != 0 {
		t.Errorf("retro.suggestion events = %d, want 0 on capture-only", n)
	}
}
