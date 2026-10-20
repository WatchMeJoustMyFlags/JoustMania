package decision

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/llm"
)

// retro_span_error_test.go is the #1206 retro-side assertion: a retro inference that
// fails (transport error / timeout) or returns an unparseable reply must set the
// agent.llm.retro span STATUS=Error with the error TYPE as description — so the failed
// retro shows up in Jaeger's error view, not only as parse_ok=false. #1242: the error
// TEXT never reaches the span (no exception event), since vendor bodies can echo keys. A successful retro leaves the span
// status Unset. Fail-open is unchanged — the span still ends cleanly either way.

// TestRetro_TransportErrorSetsSpanError: a backend whose Infer errors sets Error status
// (description = error type) on the retro span, and the error text appears nowhere on it.
// UC: UC-070 A5
func TestRetro_TransportErrorSetsSpanError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	logs := &syncBuffer{}
	rc.Log = slog.New(slog.NewTextHandler(logs, nil))
	rc.SetBackend(fakeText("claude-test",
		func(context.Context, llm.Prompt) (string, error) { return "", errors.New("boom sk-ant-LEAK") }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	// UC-070 A5 step 3: the model was called (inference.used names it), the summary
	// did not parse, llm.infer.error names the failure and a WARN log is written.
	if v, ok := attrValue(span, AttrInferenceUsed); !ok || v.AsString() != "claude" {
		t.Errorf("inference.used = %q (present=%v), want the called backend claude", v.AsString(), ok)
	}
	if v, ok := attrValue(span, AttrLLMRetroParseOK); !ok || v.AsBool() {
		t.Errorf("llm.retro.parse_ok = %v (present=%v), want false", v.AsBool(), ok)
	}
	if v, ok := attrValue(span, AttrLLMInferError); !ok || v.AsString() != "_OTHER" {
		t.Errorf("llm.infer.error = %q (present=%v), want the error type _OTHER", v.AsString(), ok)
	}
	if n := logs.count("level=WARN msg=" + SpanLLMRetroFailed); n != 1 {
		t.Errorf("WARN %s logs = %d, want 1", SpanLLMRetroFailed, n)
	}
	if n := logs.count("sk-ant-LEAK"); n != 0 {
		t.Error("the retro_failed log must carry the error type, never the error text")
	}
	if got := span.Status().Code; got != codes.Error {
		t.Errorf("retro span status = %v, want Error on a transport failure (#1206)", got)
	}
	if span.Status().Description != "_OTHER" {
		t.Errorf("retro span status description = %q, want the error type _OTHER", span.Status().Description)
	}
	if hasExceptionEvent(span) {
		t.Error("retro span must not carry an exception event (it would hold the error text, #1242)")
	}
	assertNoTextOnSpan(t, span, "sk-ant-LEAK")
}

// TestRetro_ParseFailureSetsSpanError: an unparseable reply sets Error status
// (invalid_output) on the retro span.
func TestRetro_ParseFailureSetsSpanError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetBackend(fakeText("claude-test",
		func(context.Context, llm.Prompt) (string, error) { return "not json at all", nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if got := span.Status().Code; got != codes.Error {
		t.Errorf("retro span status = %v, want Error on an unparseable reply (#1206)", got)
	}
	if span.Status().Description != retroInvalidOutput {
		t.Errorf("retro span status description = %q, want %q", span.Status().Description, retroInvalidOutput)
	}
}

// TestRetro_SuccessLeavesSpanStatusUnset: a parsed, healthy retro leaves the span status
// Unset — the #1206 change must NOT error-tag a successful retrospective.
func TestRetro_SuccessLeavesSpanStatusUnset(t *testing.T) {
	resetRetroResponseRef()
	reply := `{"session_assessment":"close finish","suggestions":[],"session_focus":"endurance"}`
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetBackend(fakeText("claude-test",
		func(context.Context, llm.Prompt) (string, error) { return reply, nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if got := span.Status().Code; got != codes.Unset {
		t.Errorf("retro span status = %v, want Unset on success (no false error tagging)", got)
	}
	if hasExceptionEvent(span) {
		t.Error("retro span must carry NO exception event on success")
	}
}

// assertNoTextOnSpan fails when needle appears in any attribute or event attribute.
func assertNoTextOnSpan(t *testing.T, span sdktrace.ReadOnlySpan, needle string) {
	t.Helper()
	for _, kv := range span.Attributes() {
		if strings.Contains(kv.Value.Emit(), needle) {
			t.Errorf("attribute %s leaks %q", kv.Key, needle)
		}
	}
	for _, ev := range span.Events() {
		for _, kv := range ev.Attributes {
			if strings.Contains(kv.Value.Emit(), needle) {
				t.Errorf("event %s attribute %s leaks %q", ev.Name, kv.Key, needle)
			}
		}
	}
}

// hasExceptionEvent reports whether the span carries a RecordError exception event.
func hasExceptionEvent(span sdktrace.ReadOnlySpan) bool {
	for _, ev := range span.Events() {
		if ev.Name == "exception" {
			return true
		}
	}
	return false
}
