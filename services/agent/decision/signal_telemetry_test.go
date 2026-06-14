package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// meteredTestLoop builds an enabled, permissive Loop wired with BOTH a recording
// tracer and a metered agent_evaluations_total counter, so a single OnEvaluate can
// be asserted against the span name AND the eval-cycle counter (#1053).
func meteredTestLoop(t *testing.T) (*Loop, *tracetest.SpanRecorder, *metric.ManualReader) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	fl := &settableFlags{snap: flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"noop", "grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
	}}
	l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.evaluations = newEvaluationsCounter(mp)
	return l, sr, reader
}

// evaluationsBySignalOutcome collects agent_evaluations_total into a
// "<signal>/<outcome>" -> count map.
func evaluationsBySignalOutcome(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_evaluations_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				signal, _ := dp.Attributes.Value("signal")
				outcome, _ := dp.Attributes.Value("outcome")
				out[signal.AsString()+"/"+outcome.AsString()] = dp.Value
			}
		}
	}
	return out
}

// traceTrigger mirrors testTrigger but for a trace-signal-driven cycle (#1053):
// the root span must be signal-agnostic, so a trace-triggered cycle emits the same
// agent.signal_received name, discriminated only by otlp.signal=traces.
func traceTrigger() EvalTrigger {
	t := testTrigger()
	t.Signal = "traces"
	t.RPCService = "opentelemetry.proto.collector.trace.v1.TraceService"
	return t
}

// TestSignalReceived_NameIsSignalAgnostic asserts the root span is now named
// agent.signal_received (NOT agent.span_received) for BOTH a metric-triggered and a
// trace-triggered eval that decides, with the correct otlp.signal discriminator.
func TestSignalReceived_NameIsSignalAgnostic(t *testing.T) {
	for _, tc := range []struct {
		name   string
		trig   EvalTrigger
		signal string
	}{
		{"metrics", testTrigger(), "metrics"},
		{"traces", traceTrigger(), "traces"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, sr, _ := meteredTestLoop(t)
			l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
			l.Actions = &fakeSink{}

			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, tc.trig)

			roots := spansByName(sr.Ended(), SignalReceived)
			if len(roots) != 1 {
				t.Fatalf("agent.signal_received spans = %d, want 1", len(roots))
			}
			if got := roots[0].Name(); got != "agent.signal_received" {
				t.Errorf("root span name = %q, want agent.signal_received", got)
			}
			if old := spansByName(sr.Ended(), "agent.span_received"); len(old) != 0 {
				t.Errorf("found %d stale agent.span_received spans, want 0", len(old))
			}
			v, ok := attrValue(roots[0], "otlp.signal")
			if !ok || v.AsString() != tc.signal {
				t.Errorf("otlp.signal = %q (present=%v), want %q", v.AsString(), ok, tc.signal)
			}
		})
	}
}

// TestEvaluationsCounter_DecidedAndIdle asserts the per-cycle counter (#1053)
// increments {signal=metrics,outcome=decided} on a metric cycle that fires a rule,
// {signal=metrics,outcome=idle} on a metric cycle that fires NOTHING, and
// {signal=traces,outcome=decided} on a trace cycle — proving signal arrival is
// observable even on idle cycles where no audit span emits.
func TestEvaluationsCounter_DecidedAndIdle(t *testing.T) {
	l, sr, reader := meteredTestLoop(t)

	// 1. Metric-triggered cycle that DECIDES.
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
	l.Actions = &fakeSink{}
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

	// 2. Metric-triggered cycle that fires NO rule (idle). No span must emit for it.
	l.Rules = fakeRules{out: nil}
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

	// 3. Trace-triggered cycle that DECIDES.
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "x"}}}
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, traceTrigger())

	counts := evaluationsBySignalOutcome(t, reader)
	for key, want := range map[string]int64{
		"metrics/decided": 1,
		"metrics/idle":    1,
		"traces/decided":  1,
	} {
		if counts[key] != want {
			t.Errorf("agent_evaluations_total{%s} = %d, want %d (all: %v)", key, counts[key], want, counts)
		}
	}

	// The idle cycle must NOT have emitted any span — the counter is the only
	// record of it (the whole point of #1053).
	if idleRoots := len(spansByName(sr.Ended(), SignalReceived)); idleRoots != 2 {
		t.Errorf("agent.signal_received spans = %d, want 2 (idle cycle emits none)", idleRoots)
	}
}
