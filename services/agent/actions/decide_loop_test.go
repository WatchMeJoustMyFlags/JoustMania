package actions

import (
	"context"
	"sync"
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
// to end in-process: what the loop records versus what reaches the flag service.

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
func meteredLoop(t *testing.T, snap flags.Snapshot, rules decision.RulesEngine, gateOn bool) (*decision.Loop, *fakeService, *metric.ManualReader, *writerSpy) {
	t.Helper()
	reader := metric.NewManualReader()
	prev := otel.GetMeterProvider()
	otel.SetMeterProvider(metric.NewMeterProvider(metric.WithReader(reader)))
	t.Cleanup(func() { otel.SetMeterProvider(prev) })

	w, svc := newTestWriter(t)
	l := decision.NewLoop(staticFlags(snap), discardLogger())
	l.Rules = rules
	spy := &writerSpy{inner: w}
	l.Actions = NewGatedActionSink(staticGate(gateOn), spy, discardLogger())
	return l, svc, reader, spy
}

// writerSpy wraps the real Writer and records each call and its returned error, so
// a test can tell "the Writer ran and succeeded" from "the gate skipped it".
type writerSpy struct {
	inner *Writer
	mu    sync.Mutex
	errs  []error
}

func (s *writerSpy) Apply(ctx context.Context, d decision.Decision) error {
	err := s.inner.Apply(ctx, d)
	s.mu.Lock()
	s.errs = append(s.errs, err)
	s.mu.Unlock()
	return err
}

func (s *writerSpy) calls() []error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]error(nil), s.errs...)
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

// assertNoWrites checks the flag service received no write.
func assertNoWrites(t *testing.T, svc *fakeService) {
	t.Helper()
	if calls := svc.all(); len(calls) != 0 {
		t.Fatalf("flag service received %d write(s), want none: %+v", len(calls), calls)
	}
}

func testTrigger() decision.EvalTrigger {
	return decision.EvalTrigger{Signal: "metrics", T0: time.Now()}
}

// TestDecideLoop_ActuationGateOff: with interventions_enabled off, a decision that
// passes the permission chain is recorded as dispatched and counted as applied
// (D-A9), but the gated sink never calls the Writer: the flag service receives
// no write.
// UC: UC-040 A11
// UC: UC-045 A1
func TestDecideLoop_ActuationGateOff(t *testing.T) {
	t.Run("UC-040/A11 gate off: dispatched, counted, nothing written", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, reader, spy := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, false)

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s-1"}, testTrigger())

		if len(state.Outcomes) != 1 || !state.Outcomes[0].Dispatched {
			t.Fatalf("outcomes = %+v, want one dispatched decision", state.Outcomes)
		}
		assertNoWrites(t, svc)
		if n := appliedTotal(t, reader, decision.InterventionPlayAudioCue); n != 1 {
			t.Fatalf("agent_interventions_applied_total{action=play_audio_cue} = %d, want 1 (D-A9)", n)
		}
		if calls := spy.calls(); len(calls) != 0 {
			t.Fatalf("Writer calls = %d, want 0 (the gate must skip the Writer)", len(calls))
		}
	})
}

// TestDecideLoop_ProbeMode: AGENT_PROBE_DECISIONS swaps the rules engine for
// ProbeRules; with the `probe` allow-list (noop only) its synthetic noop passes the
// permission chain and the real Writer treats it as a successful write that
// changes no flag, so the whole decide-and-act path runs without a game effect.
// UC: UC-040 A23
func TestDecideLoop_ProbeMode(t *testing.T) {
	t.Run("UC-040/A23 probe noop is dispatched and writes no flag", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionNoop}, // the `probe` variant
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, reader, spy := meteredLoop(t, snap, decision.NewProbeRules(5*time.Second, nil), true)

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s-1"}, testTrigger())

		if len(state.Outcomes) != 1 {
			t.Fatalf("outcomes = %+v, want the one probe decision", state.Outcomes)
		}
		if o := state.Outcomes[0]; o.Intervention != decision.InterventionNoop || !o.Dispatched {
			t.Fatalf("outcome = %+v, want a dispatched noop (not blocked)", o)
		}
		assertNoWrites(t, svc)
		if n := appliedTotal(t, reader, decision.InterventionNoop); n != 1 {
			t.Fatalf("agent_interventions_applied_total{action=noop} = %d, want 1 (a successful write)", n)
		}
		// The Writer itself ran (gate on) and accepted the noop as a successful
		// write; without this the test could not tell it from a gate skip.
		if calls := spy.calls(); len(calls) != 1 || calls[0] != nil {
			t.Fatalf("Writer calls = %v, want exactly one returning nil", calls)
		}
	})
}
