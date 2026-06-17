package decision

import (
	"context"
	"errors"
	"testing"

	"go.opentelemetry.io/otel/codes"

	"github.com/joustmania/agent/llm"
)

// retro_span_error_test.go is the #1206 retro-side assertion: a retro inference that
// fails (transport error / timeout) or returns an unparseable reply must set the
// agent.llm.retro span STATUS=Error and RecordError — so the failed retro shows up in
// Jaeger's error view, not only as parse_ok=false. A successful retro leaves the span
// status Unset. Fail-open is unchanged — the span still ends cleanly either way.

// TestRetro_TransportErrorSetsSpanError: a backend whose Infer errors sets Error status
// + an exception event on the retro span (in addition to the existing parse_ok=false).
func TestRetro_TransportErrorSetsSpanError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return "", errors.New("boom") }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if got := span.Status().Code; got != codes.Error {
		t.Errorf("retro span status = %v, want Error on a transport failure (#1206)", got)
	}
	if span.Status().Description != "boom" {
		t.Errorf("retro span status description = %q, want the transport error message", span.Status().Description)
	}
	if !hasExceptionEvent(span) {
		t.Error("retro span missing RecordError exception event on transport error")
	}
}

// TestRetro_ParseFailureSetsSpanError: an unparseable reply sets Error status + an
// exception event on the retro span.
func TestRetro_ParseFailureSetsSpanError(t *testing.T) {
	resetRetroResponseRef()
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
		func(context.Context, llm.Prompt) (string, error) { return "not json at all", nil }))

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()

	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	if got := span.Status().Code; got != codes.Error {
		t.Errorf("retro span status = %v, want Error on an unparseable reply (#1206)", got)
	}
	if !hasExceptionEvent(span) {
		t.Error("retro span missing RecordError exception event on parse failure")
	}
}

// TestRetro_SuccessLeavesSpanStatusUnset: a parsed, healthy retro leaves the span status
// Unset — the #1206 change must NOT error-tag a successful retrospective.
func TestRetro_SuccessLeavesSpanStatusUnset(t *testing.T) {
	resetRetroResponseRef()
	reply := `{"session_assessment":"close finish","suggestions":[],"session_focus":"endurance"}`
	rc, sr := recordingRetro(t, retroFlagSnapshot())
	rc.SetResolver(reachableRetroResolver(t, "gemma4:latest",
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
