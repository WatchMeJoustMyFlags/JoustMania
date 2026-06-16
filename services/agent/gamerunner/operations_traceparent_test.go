package gamerunner

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/metadata"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// withTraceContextPropagator installs the same W3C TraceContext propagator the
// agent configures in otel.go for the duration of a test, then restores the
// prior global. Without a configured propagator Inject is a no-op (the graceful
// telemetry-off path), so the positive-propagation tests must set one.
func withTraceContextPropagator(t *testing.T) {
	t.Helper()
	prev := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(prev) })
}

// experimentSpanContext builds a VALID remote SpanContext standing in for the
// agent's long-lived experiment ROOT span.
func experimentSpanContext(t *testing.T) trace.SpanContext {
	t.Helper()
	tid, err := trace.TraceIDFromHex("0123456789abcdef0123456789abcdef")
	if err != nil {
		t.Fatalf("TraceIDFromHex: %v", err)
	}
	sid, err := trace.SpanIDFromHex("0123456789abcdef")
	if err != nil {
		t.Fatalf("SpanIDFromHex: %v", err)
	}
	return trace.NewSpanContext(trace.SpanContextConfig{
		TraceID:    tid,
		SpanID:     sid,
		TraceFlags: trace.FlagsSampled,
		Remote:     true,
	})
}

// TestInjectExperimentTraceparent_ValidInjects asserts a valid experiment
// SpanContext is injected as the W3C traceparent on the outgoing metadata,
// carrying the experiment's trace_id and span_id so the coordinator parents the
// game span under the experiment span.
func TestInjectExperimentTraceparent_ValidInjects(t *testing.T) {
	withTraceContextPropagator(t)
	sc := experimentSpanContext(t)

	ctx := injectExperimentTraceparent(context.Background(), sc)

	md, ok := metadata.FromOutgoingContext(ctx)
	if !ok {
		t.Fatal("no outgoing metadata after inject")
	}
	tp := md.Get("traceparent")
	if len(tp) != 1 {
		t.Fatalf("traceparent entries = %v, want exactly 1", tp)
	}
	// W3C format: 00-<trace_id>-<span_id>-<flags>. The injected parent must carry
	// the experiment trace + span ids so the coordinator nests the game under it.
	wantTrace := sc.TraceID().String()
	wantSpan := sc.SpanID().String()
	if got := tp[0]; len(got) < 55 ||
		got[3:3+32] != wantTrace ||
		got[36:36+16] != wantSpan {
		t.Fatalf("traceparent = %q, want trace_id=%s span_id=%s", got, wantTrace, wantSpan)
	}
}

// TestInjectExperimentTraceparent_InvalidNoOp asserts the zero (invalid)
// SpanContext — a real game or an unbound shadow game — injects NOTHING, so the
// coordinator sees no incoming traceparent and keeps the game own-rooted.
func TestInjectExperimentTraceparent_InvalidNoOp(t *testing.T) {
	withTraceContextPropagator(t)

	ctx := injectExperimentTraceparent(context.Background(), trace.SpanContext{})

	if md, ok := metadata.FromOutgoingContext(ctx); ok {
		if tp := md.Get("traceparent"); len(tp) != 0 {
			t.Fatalf("traceparent injected for invalid SpanContext: %v", tp)
		}
	}
}

// TestInjectExperimentTraceparent_PreservesExistingMetadata asserts the inject
// merges onto, rather than replaces, any pre-existing outgoing metadata.
func TestInjectExperimentTraceparent_PreservesExistingMetadata(t *testing.T) {
	withTraceContextPropagator(t)
	sc := experimentSpanContext(t)

	base := metadata.NewOutgoingContext(context.Background(),
		metadata.Pairs("flagd-selector", "flagSetId=game"))
	ctx := injectExperimentTraceparent(base, sc)

	md, ok := metadata.FromOutgoingContext(ctx)
	if !ok {
		t.Fatal("no outgoing metadata after inject")
	}
	if got := md.Get("flagd-selector"); len(got) != 1 || got[0] != "flagSetId=game" {
		t.Errorf("pre-existing metadata lost: flagd-selector=%v", got)
	}
	if tp := md.Get("traceparent"); len(tp) != 1 {
		t.Errorf("traceparent not added alongside existing metadata: %v", tp)
	}
}

// TestStartExperimentGame_InjectsTraceparent is the cross-service-flow assertion:
// an experiment-bound spawn whose Spec carries a valid experiment SpanContext
// reaches the coordinator with the experiment traceparent on the incoming
// metadata. The fakeCoord captures it from the stream context.
func TestStartExperimentGame_InjectsTraceparent(t *testing.T) {
	withTraceContextPropagator(t)
	sc := experimentSpanContext(t)

	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_exp123",
		events: []*gcpb.GameEvent{ev("game_started")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	driveCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	spec := Spec{
		RunID:                 "run-exp",
		GameName:              "JoustFFA",
		Players:               2,
		Sensitivity:           2,
		ExperimentID:          "exp-1",
		Arm:                   "experimental",
		ExperimentSpanContext: sc,
	}
	if _, err := h.runner.StartExperimentGame(context.Background(), driveCtx, spec); err != nil {
		t.Fatalf("StartExperimentGame error: %v", err)
	}

	md := coord.capturedMetadata()
	if md == nil {
		t.Fatal("coordinator captured no incoming metadata")
	}
	tp := md.Get("traceparent")
	if len(tp) != 1 {
		t.Fatalf("coordinator traceparent = %v, want exactly 1 carrying the experiment span", tp)
	}
	if got := tp[0]; len(got) < 55 || got[3:3+32] != sc.TraceID().String() {
		t.Fatalf("coordinator traceparent = %q, want trace_id=%s", got, sc.TraceID())
	}
}

// TestStartExperimentGame_NoTraceparentWhenNoSpanContext is the real-game / unbound
// path: a spawn WITHOUT an experiment SpanContext injects no traceparent, so the
// coordinator keeps the game own-rooted.
func TestStartExperimentGame_NoTraceparentWhenNoSpanContext(t *testing.T) {
	withTraceContextPropagator(t)

	mock := &fakeMock{}
	coord := &fakeCoord{
		gameID: "game_plain",
		events: []*gcpb.GameEvent{ev("game_started")},
	}
	h := newHarness(t, mock, coord)
	defer h.stop()

	driveCtx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// No ExperimentSpanContext set (zero value) — mirrors a real game / unbound
	// shadow game.
	spec := Spec{RunID: "run-plain", GameName: "JoustFFA", Players: 2, Sensitivity: 2}
	if _, err := h.runner.StartExperimentGame(context.Background(), driveCtx, spec); err != nil {
		t.Fatalf("StartExperimentGame error: %v", err)
	}

	md := coord.capturedMetadata()
	if md != nil {
		if tp := md.Get("traceparent"); len(tp) != 0 {
			t.Fatalf("traceparent injected for a spawn with no experiment SpanContext: %v", tp)
		}
	}
}
