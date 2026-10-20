package actions

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
)

// write_failed_uc040_test.go drives the real decision loop into the real gated
// sink and Writer with a flag service that refuses or cannot be reached, so the
// failure side of UC-040 step 12 is checked end to end: A27 (the write fails)
// and the failure postcondition Post-F-1 (nothing reaches the flag service
// outside the allow-list, the per-game budget and the actuation gate, and no
// other game is written).

const uc040GameID = "game_0a1b2c3d4e5f"

// lockedBuffer is a goroutine-safe log sink.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

// records returns every JSON log record written so far.
func (b *lockedBuffer) records(t *testing.T) []map[string]any {
	t.Helper()
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(b.buf.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("decode log line %q: %v", line, err)
		}
		out = append(out, rec)
	}
	return out
}

// countLevel counts the records with msg at level.
func countLevel(recs []map[string]any, msg, level string) int {
	n := 0
	for _, r := range recs {
		if r["msg"] == msg && r["level"] == level {
			n++
		}
	}
	return n
}

// loggedLoop is meteredLoop with the Writer and the loop logging JSON into logs,
// and an in-memory span recorder on the loop.
func loggedLoop(t *testing.T, snap flags.Snapshot, rules decision.RulesEngine, gateOn bool) (*decision.Loop, *fakeService, *metric.ManualReader, *writerSpy, *lockedBuffer, *tracetest.SpanRecorder) {
	t.Helper()
	l, svc, reader, spy := meteredLoop(t, snap, rules, gateOn)
	logs := &lockedBuffer{}
	logger := slog.New(slog.NewJSONHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	spy.inner = NewWriter(svc, logger) // after meteredLoop set the meter provider
	l.Log = logger
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l.Tracer = tp.Tracer("test")
	return l, svc, reader, spy, logs, sr
}

func actionSpans(sr *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.Name() == decision.SpanAction {
			out = append(out, s)
		}
	}
	return out
}

// UC: UC-040 A27
func TestDecideLoop_FailedWrite(t *testing.T) {
	failures := []struct {
		name string
		err  error
	}{
		{"unreachable", errors.New("dial tcp 10.0.0.1:8070: connect: connection refused")},
		{"no answer within 500 ms", fmt.Errorf("put interventions/volume_override: %w", context.DeadlineExceeded)},
		{"503", &flagservice.Error{Status: 503, Reason: flagservice.ReasonNoSyncStream}},
		{"unknown game", &flagservice.Error{Status: 404, Reason: "unknown_game", Detail: "game " + uc040GameID + " is not registered"}},
		{"game not live after the client's one retry", &flagservice.Error{Status: 409, Reason: flagservice.ReasonGameNotLive}},
	}
	for _, f := range failures {
		t.Run("UC-040/A27 "+f.name+": counted and logged as a failed write, nothing applied, no retry", func(t *testing.T) {
			snap := flags.Snapshot{
				Enabled:              true,
				InterventionsAllowed: []string{decision.InterventionAdjustVolume},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
			}
			l, svc, reader, spy, logs, sr := loggedLoop(t, snap,
				oneDecision{Intervention: decision.InterventionAdjustVolume, Reason: "quieter"}, true)
			svc.err = f.err

			state := l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: uc040GameID}, testTrigger())

			// Step 1: the Writer sends one write and does not retry it.
			if calls := svc.all(); len(calls) != 1 {
				t.Fatalf("flag service got %d writes, want 1 (no retry)", len(calls))
			}
			if calls := spy.calls(); len(calls) != 1 || !errors.Is(calls[0], f.err) {
				t.Fatalf("Writer calls = %v, want one returning %v", calls, f.err)
			}
			// Step 2: counted, logged, span in error, not applied.
			if n := writesTotal(t, reader, resultError); n != 1 {
				t.Fatalf("agent_intervention_writes_total{result=error} = %d, want 1", n)
			}
			if n := writesTotal(t, reader, resultOK); n != 0 {
				t.Fatalf("agent_intervention_writes_total{result=ok} = %d, want 0", n)
			}
			if n := appliedTotal(t, reader, decision.InterventionAdjustVolume); n != 0 {
				t.Fatalf("agent_interventions_applied_total{action=adjust_volume} = %d, want 0", n)
			}
			recs := logs.records(t)
			if n := countLevel(recs, "agent.action_write_failed", "ERROR"); n != 1 {
				t.Fatalf("agent.action_write_failed ERROR logs = %d, want 1: %v", n, recs)
			}
			if n := countLevel(recs, "agent.apply_failed", "ERROR"); n != 1 {
				t.Fatalf("agent.apply_failed ERROR logs = %d, want 1: %v", n, recs)
			}
			spans := actionSpans(sr)
			if len(spans) != 1 || spans[0].Status().Code != codes.Error {
				t.Fatalf("agent.action spans = %v, want one with status Error", spans)
			}
			// Step 3: the decision is still recorded as dispatched.
			if len(state.Outcomes) != 1 || !state.Outcomes[0].Dispatched {
				t.Fatalf("outcomes = %+v, want one dispatched decision", state.Outcomes)
			}
		})
	}

	t.Run("UC-040/A27 a failed write has charged the per-game budget", func(t *testing.T) {
		// adjust_volume costs 0.5; a budget of 1 fits two. Both writes fail, and
		// the third decision is refused for the budget: the failures were charged.
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionAdjustVolume},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 1},
		}
		l, svc, _, _, _, _ := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionAdjustVolume, Reason: "quieter"}, true)
		svc.err = &flagservice.Error{Status: 503, Reason: flagservice.ReasonNoSyncStream}
		c := gamecontext.GameContext{SessionID: uc040GameID}

		for i := 0; i < 2; i++ {
			st := l.OnEvaluate(context.Background(), c, testTrigger())
			if len(st.Outcomes) != 1 || !st.Outcomes[0].Dispatched {
				t.Fatalf("cycle %d outcomes = %+v, want one dispatched (failed) write", i+1, st.Outcomes)
			}
		}
		st := l.OnEvaluate(context.Background(), c, testTrigger())
		if len(st.Outcomes) != 1 || st.Outcomes[0].Dispatched || st.Outcomes[0].BlockReason != decision.ReasonRateLimit {
			t.Fatalf("cycle 3 outcomes = %+v, want one blocked for rate_limit", st.Outcomes)
		}
		if n := len(svc.all()); n != 2 {
			t.Fatalf("flag service got %d writes, want 2 (the refused third never reaches it)", n)
		}
	})
}

// UC: UC-040 A11
func TestDecideLoop_GateOffChargesBudgetAndSpans(t *testing.T) {
	t.Run("UC-040/A11 gate off: the decision charges the per-game budget and records its agent.action span", func(t *testing.T) {
		// adjust_volume costs 0.5; a budget of 1 fits two. With the actuation gate
		// off nothing is written, yet the third decision is refused for the budget:
		// the two gated decisions were charged.
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionAdjustVolume},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 1},
		}
		l, svc, _, spy, _, sr := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionAdjustVolume, Reason: "quieter"}, false)
		c := gamecontext.GameContext{SessionID: uc040GameID}

		for i := 0; i < 2; i++ {
			st := l.OnEvaluate(context.Background(), c, testTrigger())
			if len(st.Outcomes) != 1 || !st.Outcomes[0].Dispatched {
				t.Fatalf("cycle %d outcomes = %+v, want one dispatched (gated) decision", i+1, st.Outcomes)
			}
		}
		if spans := actionSpans(sr); len(spans) != 2 {
			t.Fatalf("agent.action spans = %d, want 2 (a gated decision still records its span)", len(spans))
		}
		st := l.OnEvaluate(context.Background(), c, testTrigger())
		if len(st.Outcomes) != 1 || st.Outcomes[0].Dispatched || st.Outcomes[0].BlockReason != decision.ReasonRateLimit {
			t.Fatalf("cycle 3 outcomes = %+v, want one blocked for rate_limit (the gated decisions were charged)", st.Outcomes)
		}
		assertNoWrites(t, svc)
		if calls := spy.calls(); len(calls) != 0 {
			t.Fatalf("Writer calls = %d, want 0 (the gate skips the Writer)", len(calls))
		}
	})
}

// writtenGameIDs returns the game id every flag-service call was scoped to.
func writtenGameIDs(svc *fakeService) []string {
	var out []string
	for _, c := range svc.all() {
		if c.method == "POST" {
			out = append(out, c.event.Scope.GameID)
		} else {
			out = append(out, c.write.Scope.GameID)
		}
	}
	return out
}

// UC: UC-040 Post-F-1 BR-002
func TestDecideLoop_FailurePostconditionNothingWrittenOutsideTheGates(t *testing.T) {
	c := gamecontext.GameContext{SessionID: uc040GameID}

	t.Run("UC-040/Post-F-1 an intervention outside the allow-list writes nothing", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, _, spy, _, _ := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionAdjustVolume, Reason: "quieter"}, true)
		st := l.OnEvaluate(context.Background(), c, testTrigger())
		if len(st.Outcomes) != 1 || st.Outcomes[0].BlockReason != decision.ReasonNotAllowed {
			t.Fatalf("outcomes = %+v, want one blocked not_allowed", st.Outcomes)
		}
		if n := len(spy.calls()); n != 0 {
			t.Fatalf("Writer calls = %d, want 0", n)
		}
		assertNoWrites(t, svc)
	})

	t.Run("UC-040/Post-F-1 a spent per-game budget writes nothing more", func(t *testing.T) {
		// play_audio_cue costs 0.5: a budget of 1 fits two writes, the third is refused.
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 1},
		}
		l, svc, _, _, _, _ := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, true)
		for i := 0; i < 3; i++ {
			l.OnEvaluate(context.Background(), c, testTrigger())
		}
		if n := len(svc.all()); n != 2 {
			t.Fatalf("flag service got %d writes, want 2 (the budget fits two)", n)
		}
	})

	t.Run("UC-040/Post-F-1 the actuation gate off writes nothing", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionPlayAudioCue},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, _, spy, _, _ := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionPlayAudioCue, Reason: "cue"}, false)
		l.OnEvaluate(context.Background(), c, testTrigger())
		if n := len(spy.calls()); n != 0 {
			t.Fatalf("Writer calls = %d, want 0", n)
		}
		assertNoWrites(t, svc)
	})

	t.Run("UC-040/Post-F-1 a failed write reaches no other game", func(t *testing.T) {
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{decision.InterventionEndGame},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		}
		l, svc, _, _, _, _ := loggedLoop(t, snap,
			oneDecision{Intervention: decision.InterventionEndGame, Reason: "end"}, true)
		svc.err = &flagservice.Error{Status: 503, Reason: flagservice.ReasonNoSyncStream}
		l.OnEvaluate(context.Background(), c, testTrigger())
		ids := writtenGameIDs(svc)
		if len(ids) != 1 || ids[0] != uc040GameID {
			t.Fatalf("writes scoped to %v, want exactly one for %s", ids, uc040GameID)
		}
	})
}
