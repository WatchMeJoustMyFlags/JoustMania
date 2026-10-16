package actions

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// decide_loop_test.go drives the real decision loop into the real gated sink and
// Writer, so the decide path's last two steps (UC-040 steps 11-12) are checked end
// to end in-process: what the loop records versus what reaches interventions.json.

// staticFlags is a constant flag source for the loop.
type staticFlags flags.Snapshot

func (f staticFlags) Evaluate(context.Context) flags.Snapshot { return flags.Snapshot(f) }

// oneDecision is a rules engine that always returns the same decision.
type oneDecision decision.Decision

func (r oneDecision) Evaluate(context.Context, gamecontext.GameContext) []decision.Decision {
	return []decision.Decision{decision.Decision(r)}
}

// meteredLoop builds a loop over the real gated sink and Writer. The loop's
// counters bind to the global meter provider at construction, so the test points
// the global at a manual reader for the loop's lifetime and restores it after.
func meteredLoop(t *testing.T, snap flags.Snapshot, rules decision.RulesEngine, gateOn bool) (*decision.Loop, string, *metric.ManualReader) {
	t.Helper()
	reader := metric.NewManualReader()
	prev := otel.GetMeterProvider()
	otel.SetMeterProvider(metric.NewMeterProvider(metric.WithReader(reader)))
	t.Cleanup(func() { otel.SetMeterProvider(prev) })

	w, path := newTestWriter(t)
	l := decision.NewLoop(staticFlags(snap), discardLogger())
	l.Rules = rules
	l.Actions = NewGatedActionSink(staticGate(gateOn), w, discardLogger())
	return l, path, reader
}

// appliedTotal reads agent_interventions_applied_total{action}.
func appliedTotal(t *testing.T, reader *metric.ManualReader, action string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
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
				if v, _ := dp.Attributes.Value("action"); v.AsString() == action {
					return dp.Value
				}
			}
		}
	}
	return 0
}

func assertFlagsUnchanged(t *testing.T, path string, before map[string]json.RawMessage) {
	t.Helper()
	after := readAllFlagsRaw(t, path)
	for key, raw := range before {
		if string(after[key]) != string(raw) {
			t.Fatalf("flag %q changed:\n before=%s\n after =%s", key, raw, after[key])
		}
	}
}

func testTrigger() decision.EvalTrigger {
	return decision.EvalTrigger{Signal: "metrics", T0: time.Now()}
}

// TestDecideLoop_ActuationGateOff: with interventions_enabled off, a decision that
// passes the permission chain is recorded as dispatched and counted as applied
// (D-A9), but the gated sink never calls the Writer: interventions.json is
// byte-identical.
// UC: UC-040 A11
func TestDecideLoop_ActuationGateOff(t *testing.T) {
	t.Run("UC-040/A11 gate off: dispatched, counted, nothing written", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, path, reader := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, false)
		before := readAllFlagsRaw(t, path)

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s-1"}, testTrigger())

		if len(state.Outcomes) != 1 || !state.Outcomes[0].Dispatched {
			t.Fatalf("outcomes = %+v, want one dispatched decision", state.Outcomes)
		}
		assertFlagsUnchanged(t, path, before)
		if n := appliedTotal(t, reader, decision.InterventionPlayAudioCue); n != 1 {
			t.Fatalf("agent_interventions_applied_total{action=play_audio_cue} = %d, want 1 (D-A9)", n)
		}
	})
}

// TestDecideLoop_ProbeMode: AGENT_PROBE_DECISIONS swaps the rules engine for
// ProbeRules; with the `probe` allow-list (noop only) its synthetic noop passes the
// permission chain and the real Writer treats it as a successful write that
// changes no flag, so the whole decide-and-act path runs without a game effect.
// UC: UC-040 A21
func TestDecideLoop_ProbeMode(t *testing.T) {
	t.Run("UC-040/A21 probe noop is dispatched and writes no flag", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionNoop}, // the `probe` variant
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, path, reader := meteredLoop(t, snap, decision.NewProbeRules(5*time.Second, nil), true)
		before := readAllFlagsRaw(t, path)

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s-1"}, testTrigger())

		if len(state.Outcomes) != 1 {
			t.Fatalf("outcomes = %+v, want the one probe decision", state.Outcomes)
		}
		if o := state.Outcomes[0]; o.Intervention != decision.InterventionNoop || !o.Dispatched {
			t.Fatalf("outcome = %+v, want a dispatched noop (not blocked)", o)
		}
		assertFlagsUnchanged(t, path, before)
		if n := appliedTotal(t, reader, decision.InterventionNoop); n != 1 {
			t.Fatalf("agent_interventions_applied_total{action=noop} = %d, want 1 (a successful write)", n)
		}
	})
}
