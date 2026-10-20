package actions

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

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
// passes the permission chain is recorded as dispatched, but the gated sink never
// calls the Writer: the flag service receives no write and the decision is not
// counted as applied (D-A9 resolved, #1681).
// UC: UC-040 A11
// UC: UC-045 A1
func TestDecideLoop_ActuationGateOff(t *testing.T) {
	t.Run("UC-040/A11 gate off: dispatched, nothing written, not counted", func(t *testing.T) {
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
		if n := appliedTotal(t, reader, decision.InterventionPlayAudioCue); n != 0 {
			t.Fatalf("agent_interventions_applied_total{action=play_audio_cue} = %d, want 0 (a gated no-op is not applied, #1681)", n)
		}
		if calls := spy.calls(); len(calls) != 0 {
			t.Fatalf("Writer calls = %d, want 0 (the gate must skip the Writer)", len(calls))
		}
	})
	t.Run("UC-045/A1 gate off: the agent keeps deciding and spanning, writes nothing", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, _, spy := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, false)
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		l.Tracer = tp.Tracer("test")

		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s-1"}, testTrigger())

		decisions := 0
		for _, s := range sr.Ended() {
			if s.Name() == decision.SpanDecision {
				decisions++
			}
		}
		if decisions != 1 {
			t.Fatalf("agent.decision spans = %d, want 1 (observe without acting still spans)", decisions)
		}
		assertNoWrites(t, svc)
		if calls := spy.calls(); len(calls) != 0 {
			t.Fatalf("Writer calls = %d, want 0", len(calls))
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

	t.Run("UC-040/A23 probe noop: no agent.decide span, decision and action spans, charges 1.0 of the budget", func(t *testing.T) {
		// A budget of 2 admits two noops and refuses the third: the noop costs
		// more than 2/3 and at most 1.0 (the medium weight).
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionNoop},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 2},
		}
		clock := time.Unix(1000, 0)
		probe := decision.NewProbeRules(5*time.Second, func() time.Time { return clock })
		l, _, _, _, _, sr := loggedLoop(t, snap, probe, true)
		c := gamecontext.GameContext{SessionID: "s-1"}

		var outcomes []decision.DecisionOutcome
		for i := 0; i < 3; i++ {
			st := l.OnEvaluate(context.Background(), c, testTrigger())
			if len(st.Outcomes) != 1 {
				t.Fatalf("cycle %d outcomes = %+v, want the one probe decision", i+1, st.Outcomes)
			}
			outcomes = append(outcomes, st.Outcomes[0])
			clock = clock.Add(6 * time.Second) // past the probe's 5 s cadence
		}
		if !outcomes[0].Dispatched || !outcomes[1].Dispatched {
			t.Fatalf("outcomes = %+v, want the first two noops dispatched", outcomes)
		}
		if outcomes[2].Dispatched || outcomes[2].BlockReason != decision.ReasonRateLimit {
			t.Fatalf("third outcome = %+v, want blocked for rate_limit (each noop charged 1.0)", outcomes[2])
		}

		counts := map[string]int{}
		for _, s := range sr.Ended() {
			counts[s.Name()]++
		}
		if counts[decision.SpanDecide] != 0 {
			t.Fatalf("agent.decide spans = %d, want 0 (probe mode has no rules answers)", counts[decision.SpanDecide])
		}
		if counts[decision.SpanDecision] < 2 || counts[decision.SpanAction] < 2 {
			t.Fatalf("spans = %v, want an agent.decision and an agent.action span per dispatched noop", counts)
		}
	})
}

// writesTotal reads agent_intervention_writes_total{result}.
func writesTotal(t *testing.T, reader *metric.ManualReader, result string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_intervention_writes_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				if v, _ := dp.Attributes.Value("result"); v.AsString() == result {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// TestDecideLoop_RefusesNonGameID: a decision taken on the partition for
// unlabeled signals (id `session-<n>`, not a coordinator game id) reaches the
// real Writer, which refuses it before any flag-service call: one failed write
// counted, the agent.action span in error, nothing applied and no retry.
// UC: UC-040 A18 BR-002
func TestDecideLoop_RefusesNonGameID(t *testing.T) {
	t.Run("UC-040/A18 end_game on a session-<n> partition is refused", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionEndGame},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, reader, spy := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionEndGame, Reason: "end"}, true)
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
		l.Tracer = tp.Tracer("test")

		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "session-1"}, testTrigger())

		assertNoWrites(t, svc)
		if calls := spy.calls(); len(calls) != 1 || !errors.Is(calls[0], errNoGame) {
			t.Fatalf("Writer calls = %v, want exactly one refusal (errNoGame, no retry)", calls)
		}
		if n := writesTotal(t, reader, resultError); n != 1 {
			t.Fatalf("agent_intervention_writes_total{result=error} = %d, want 1", n)
		}
		if n := appliedTotal(t, reader, decision.InterventionEndGame); n != 0 {
			t.Fatalf("agent_interventions_applied_total{action=end_game} = %d, want 0", n)
		}
		var action sdktrace.ReadOnlySpan
		for _, s := range sr.Ended() {
			if s.Name() == decision.SpanAction {
				action = s
			}
		}
		if action == nil || action.Status().Code != codes.Error {
			t.Fatalf("agent.action span = %v, want one with status Error", action)
		}
	})
}

// TestDecideLoop_DisabledDecidesNothing: with `enabled` off the loop stops at the
// existence layer: the rules never decide, the Writer is never called and the
// flag service receives nothing, even with the actuation gate on.
// UC: UC-040 A28
func TestDecideLoop_DisabledDecidesNothing(t *testing.T) {
	t.Run("UC-040/A28 enabled=false makes no decision and writes nothing", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              false,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, _, spy := meteredLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, true)

		state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "game_0a1b2c3d4e5f"}, testTrigger())

		if len(state.Outcomes) != 0 {
			t.Fatalf("outcomes = %+v, want none (the kill switch decides nothing)", state.Outcomes)
		}
		if calls := spy.calls(); len(calls) != 0 {
			t.Fatalf("Writer calls = %d, want 0", len(calls))
		}
		assertNoWrites(t, svc)
	})
}
