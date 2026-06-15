package decision

import (
	"context"
	"errors"
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

// decisionMetricsLoop builds an enabled Loop wired with a recording tracer and BOTH
// #790 counters (agent_decisions_total + agent_interventions_applied_total) reading
// the same manual reader, so a single OnEvaluate can be asserted against both. The
// caller supplies the flag snapshot so each test can exercise a specific permission
// gate (allow-list / battery / rate limit) or the applied path.
func decisionMetricsLoop(t *testing.T, snap flags.Snapshot) (*Loop, *fakeSink, *metric.ManualReader) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	fl := &settableFlags{snap: snap}
	l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.decisions = newDecisionsCounter(mp)
	l.interventionsApplied = newInterventionsAppliedCounter(mp)
	sink := &fakeSink{}
	l.Actions = sink
	return l, sink, reader
}

// decisionsByLabels collects agent_decisions_total into an
// "<action>/<blocked>/<block_reason>" -> count map.
func decisionsByLabels(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_decisions_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				action, _ := dp.Attributes.Value("action")
				blocked, _ := dp.Attributes.Value("blocked")
				reason, _ := dp.Attributes.Value("block_reason")
				key := action.AsString() + "/" + blocked.Emit() + "/" + reason.AsString()
				out[key] = dp.Value
			}
		}
	}
	return out
}

// appliedByAction collects agent_interventions_applied_total into an action -> count map.
func appliedByAction(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_interventions_applied_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				action, _ := dp.Attributes.Value("action")
				out[action.AsString()] = dp.Value
			}
		}
	}
	return out
}

// permissiveSnap is an enabled, rules-mode snapshot that allows grant_shield with a
// generous rate budget — the baseline for the applied path.
func permissiveSnap() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
	}
}

// TestDecisionsCounter_Applied: a decision that passes every gate and applies
// cleanly is counted once in agent_decisions_total with blocked=false / empty reason
// AND once in agent_interventions_applied_total.
func TestDecisionsCounter_Applied(t *testing.T) {
	l, sink, reader := decisionMetricsLoop(t, permissiveSnap())
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

	if got := decisionsByLabels(t, reader)["grant_shield/false/"]; got != 1 {
		t.Errorf("agent_decisions_total{grant_shield,false,} = %d, want 1 (all: %v)", got, decisionsByLabels(t, reader))
	}
	if got := appliedByAction(t, reader)["grant_shield"]; got != 1 {
		t.Errorf("agent_interventions_applied_total{grant_shield} = %d, want 1", got)
	}
	if n := sink.calls.Load(); n != 1 {
		t.Errorf("sink Apply calls = %d, want 1", n)
	}
}

// TestDecisionsCounter_BlockReasons: each permission gate increments
// agent_decisions_total with blocked=true and the correct block_reason, and NEVER
// increments the applied counter (the decision was never dispatched).
func TestDecisionsCounter_BlockReasons(t *testing.T) {
	tests := []struct {
		name   string
		snap   flags.Snapshot
		ctx    gamecontext.GameContext
		dec    Decision
		reason string
	}{
		{
			name: "not_allowed",
			// grant_shield NOT in the allow-list -> ReasonNotAllowed.
			snap: flags.Snapshot{
				Enabled:              true,
				Mode:                 "rules",
				InterventionsAllowed: []string{"noop"},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
			},
			ctx:    gamecontext.GameContext{SessionID: "s"},
			dec:    Decision{Intervention: "grant_shield", Reason: "x"},
			reason: "not_allowed",
		},
		{
			name: "battery_threshold",
			// Player-targeted, allowed, but target battery below threshold.
			snap: flags.Snapshot{
				Enabled:              true,
				Mode:                 "rules",
				InterventionsAllowed: []string{"grant_shield"},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 100, BatteryThreshold: 50},
			},
			ctx: gamecontext.GameContext{
				SessionID: "s",
				Players: map[string]*gamecontext.PlayerSignals{
					"MOCK0001": {BatteryPct: ptrF(10)},
				},
			},
			dec:    Decision{Intervention: "grant_shield", TargetSerial: "MOCK0001", Reason: "x"},
			reason: "battery_threshold",
		},
		{
			name: "rate_limit",
			// Allowed, no battery gate, but the per-minute budget is 0 -> exhausted.
			snap: flags.Snapshot{
				Enabled:              true,
				Mode:                 "rules",
				InterventionsAllowed: []string{"grant_shield"},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 0},
			},
			ctx:    gamecontext.GameContext{SessionID: "s"},
			dec:    Decision{Intervention: "grant_shield", Reason: "x"},
			reason: "rate_limit",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			l, sink, reader := decisionMetricsLoop(t, tc.snap)
			l.Rules = fakeRules{out: []Decision{tc.dec}}

			l.OnEvaluate(context.Background(), tc.ctx, testTrigger())

			key := tc.dec.Intervention + "/true/" + tc.reason
			counts := decisionsByLabels(t, reader)
			if counts[key] != 1 {
				t.Errorf("agent_decisions_total{%s} = %d, want 1 (all: %v)", key, counts[key], counts)
			}
			if applied := appliedByAction(t, reader); len(applied) != 0 {
				t.Errorf("agent_interventions_applied_total non-empty on block: %v", applied)
			}
			if n := sink.calls.Load(); n != 0 {
				t.Errorf("sink Apply calls = %d, want 0 (blocked decision is never applied)", n)
			}
		})
	}
}

// TestDecisionsCounter_IdleCycleNoIncrement: a cycle where the engine returns no
// decision (idle) touches neither #790 counter — they count per-decision, and there
// was no decision.
func TestDecisionsCounter_IdleCycleNoIncrement(t *testing.T) {
	l, _, reader := decisionMetricsLoop(t, permissiveSnap())
	l.Rules = fakeRules{out: nil}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

	if d := decisionsByLabels(t, reader); len(d) != 0 {
		t.Errorf("agent_decisions_total non-empty on idle cycle: %v", d)
	}
	if a := appliedByAction(t, reader); len(a) != 0 {
		t.Errorf("agent_interventions_applied_total non-empty on idle cycle: %v", a)
	}
}

// TestDecisionsCounter_ApplyErrorNotApplied: a decision that passes every gate but
// whose ActionSink.Apply returns an error IS counted in agent_decisions_total
// (blocked=false — no gate refused it) but is NOT counted as applied (the dispatch
// failed).
func TestDecisionsCounter_ApplyErrorNotApplied(t *testing.T) {
	l, sink, reader := decisionMetricsLoop(t, permissiveSnap())
	sink.err = errors.New("boom")
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

	if got := decisionsByLabels(t, reader)["grant_shield/false/"]; got != 1 {
		t.Errorf("agent_decisions_total{grant_shield,false,} = %d, want 1", got)
	}
	if a := appliedByAction(t, reader); len(a) != 0 {
		t.Errorf("agent_interventions_applied_total non-empty after apply error: %v", a)
	}
}

// TestMetricAction_EmptyIsNoop: an empty intervention id is labeled "noop" so the
// action label stays a small closed set.
func TestMetricAction_EmptyIsNoop(t *testing.T) {
	if got := metricAction(""); got != "noop" {
		t.Errorf("metricAction(\"\") = %q, want noop", got)
	}
	if got := metricAction("grant_shield"); got != "grant_shield" {
		t.Errorf("metricAction(grant_shield) = %q, want grant_shield", got)
	}
}
