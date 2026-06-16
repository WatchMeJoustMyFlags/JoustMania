package decision

import (
	"context"
	"errors"
	"testing"
	"time"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// async_span_error_test.go is the #1206 assertion: the async inference failure paths
// (timeout / infer-error / unparseable) must set span STATUS=Error and RecordError on
// the agent.llm.infer attribution span — so the fail-open degrade-to-rules outcome is
// VISIBLE in Jaeger's error view, not buried in parse_ok=false attributes. The
// outbound agent.llm.infer.call span is also error-stamped on a transport failure (the
// span active when "context deadline exceeded" actually occurs). A SUCCESS leaves the
// infer span status Unset. Control flow is unchanged — these tests also confirm rules
// still decided / the result still applied.

// hasExceptionEvent reports whether the span carries an OTel "exception" event (what
// span.RecordError emits), proving RecordError ran on the failure branch.
func hasExceptionEvent(span sdktrace.ReadOnlySpan) bool {
	for _, ev := range span.Events() {
		if ev.Name == "exception" {
			return true
		}
	}
	return false
}

// TestAsync_TimeoutSetsSpanError: on a latency-budget timeout the agent.llm.infer span
// status is Error and carries an exception event, while fail-open is preserved (rules
// still decided and dispatched).
func TestAsync_TimeoutSetsSpanError(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	snap := asyncSnapshot()
	snap.LLMGate.LatencyBudget = 20 * time.Millisecond
	l, sr, sink := asyncLoop(t, snap, resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules on timeout"}})

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	l.AwaitInflight()
	close(be.release)

	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if got := inf[0].Status().Code; got != codes.Error {
		t.Errorf("infer span status = %v, want Error (#1206 timeout visible in error view)", got)
	}
	// The outbound-call span (where the deadline actually fires) is also error-stamped.
	call := spansByName(sr.Ended(), SpanLLMInferCall)
	if len(call) != 1 {
		t.Fatalf("agent.llm.infer.call spans = %d, want 1", len(call))
	}
	if got := call[0].Status().Code; got != codes.Error {
		t.Errorf("infer.call span status = %v, want Error on a timed-out outbound call", got)
	}
	if !hasExceptionEvent(call[0]) {
		t.Error("infer.call span missing RecordError exception event on timeout")
	}
	// Fail-open unchanged: rules still decided and dispatched.
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules fallback still dispatched)", sink.calls.Load())
	}
}

// TestAsync_InferErrorSetsSpanError: a transport error sets Error status + exception on
// both the infer attribution span and the outbound-call span; rules still decided.
func TestAsync_InferErrorSetsSpanError(t *testing.T) {
	be := newBlockingBackend("phi4-mini", "")
	be.err = errors.New("backend exploded")
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules on error"}})

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if got := inf[0].Status().Code; got != codes.Error {
		t.Errorf("infer span status = %v, want Error on a transport failure", got)
	}
	if inf[0].Status().Description != "backend exploded" {
		t.Errorf("infer span status description = %q, want the transport error message", inf[0].Status().Description)
	}
	if !hasExceptionEvent(inf[0]) {
		t.Error("infer span missing RecordError exception event on infer error")
	}
	call := spansByName(sr.Ended(), SpanLLMInferCall)
	if len(call) != 1 || call[0].Status().Code != codes.Error {
		t.Errorf("infer.call span must be Error-stamped on a transport failure")
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules fallback still dispatched)", sink.calls.Load())
	}
}

// TestAsync_UnparseableSetsSpanError: a reachable backend returning garbage (clean
// transport, undecodable body) sets Error status + exception on the infer attribution
// span; the outbound-call span stays Unset (the transport itself succeeded); rules
// still decided.
func TestAsync_UnparseableSetsSpanError(t *testing.T) {
	be := newBlockingBackend("phi4-mini", "not json at all")
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "rules on unparseable"}})

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if got := inf[0].Status().Code; got != codes.Error {
		t.Errorf("infer span status = %v, want Error on an unparseable reply", got)
	}
	if !hasExceptionEvent(inf[0]) {
		t.Error("infer span missing RecordError exception event on unparseable reply")
	}
	// The transport succeeded — only the body was bad — so the call span stays Unset.
	call := spansByName(sr.Ended(), SpanLLMInferCall)
	if len(call) != 1 {
		t.Fatalf("agent.llm.infer.call spans = %d, want 1", len(call))
	}
	if got := call[0].Status().Code; got != codes.Unset {
		t.Errorf("infer.call span status = %v, want Unset (transport succeeded)", got)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules fallback still dispatched)", sink.calls.Load())
	}
}

// TestAsync_SuccessLeavesSpanStatusUnset: a fresh, applied result leaves the infer span
// status Unset (no error) — the #1206 change must NOT error-tag healthy inferences.
func TestAsync_SuccessLeavesSpanStatusUnset(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, sink := asyncLoop(t, asyncSnapshot(), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	close(be.release)
	l.AwaitInflight()

	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if got := inf[0].Status().Code; got != codes.Unset {
		t.Errorf("infer span status = %v, want Unset on success (no false error tagging)", got)
	}
	if hasExceptionEvent(inf[0]) {
		t.Error("infer span must carry NO exception event on success")
	}
	call := spansByName(sr.Ended(), SpanLLMInferCall)
	if len(call) != 1 || call[0].Status().Code != codes.Unset {
		t.Errorf("infer.call span status must be Unset on success")
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (llm decision dispatched)", sink.calls.Load())
	}
}
