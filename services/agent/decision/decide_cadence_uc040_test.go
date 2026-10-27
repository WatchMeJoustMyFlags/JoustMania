package decision

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// Remote decide cadence (#2123): the next remote call of a game starts no
// earlier than the end of the previous call (answer, timeout or failure) plus
// decision.min_gap_after_answer_ms; an answer built on a state older than
// decision.max_state_age_ms is discarded; every discarded answer is counted in
// agent_decide_discarded_total{reason}.

// lockedClock is a mutex-guarded fake clock for the Loop: the remote-call
// goroutine reads it (call end, gap clock) while the test advances it.
type lockedClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *lockedClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *lockedClock) add(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// cadenceHarness is a remote harness on a fake loop clock with the gap flag set
// and the fire-time cadence (decision.min_interval_ms) off, so only the gap and
// the in-flight guard hold calls back.
func cadenceHarness(t *testing.T, gap time.Duration, be *fakeDecideBackend) (remoteHarness, *lockedClock, *metric.ManualReader) {
	t.Helper()
	snap := remoteSnapshot(time.Hour)
	snap.Decision.MinInterval = 0
	snap.Decision.MinGapAfterAnswer = gap
	h := newRemoteHarness(t, snap, be)
	clk := &lockedClock{t: time.Unix(30_000, 0)}
	h.loop.now = clk.now
	reader := metric.NewManualReader()
	h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
	return h, clk, reader
}

// cycleAndWait runs one evaluation cycle and waits for any fired call to land.
func cycleAndWait(h remoteHarness) {
	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	h.loop.AwaitInflight()
}

// NFR: NFR-012
// UC: UC-040 A4
func TestRemote_NoCallBeforeAnswerPlusGap(t *testing.T) {
	t.Run("UC-040/A4 the next call waits for the previous answer plus decision.min_gap_after_answer_ms", func(t *testing.T) {
		var clk *lockedClock
		// The call takes 5 s on the loop clock: the answer lands at t0+5s.
		be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
			clk.add(5 * time.Second)
			return ffaAnswer("noop", 0.9, "none", 0, "")(decide.Request{})
		})
		h, c, reader := cadenceHarness(t, time.Second, be)
		clk = c
		cycleAndWait(h)
		if be.callCount() != 1 {
			t.Fatalf("calls = %d, want 1", be.callCount())
		}
		clk.add(900 * time.Millisecond) // answer + 0.9 s: inside the gap
		cycleAndWait(h)
		if be.callCount() != 1 {
			t.Fatalf("calls = %d, want 1: a call started 0.9 s after the answer (gap 1 s)", be.callCount())
		}
		if got := gatedByReason(t, reader); got[DecideGateInterval] != 1 {
			t.Fatalf("agent_llm_gated_total = %v, want {decide_interval: 1}", got)
		}
		clk.add(200 * time.Millisecond) // answer + 1.1 s
		cycleAndWait(h)
		if be.callCount() != 2 {
			t.Fatalf("calls = %d, want 2 once answer + gap has passed", be.callCount())
		}
	})
	t.Run("UC-040/A4 gap 0 disables the clock", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		h, _, _ := cadenceHarness(t, 0, be)
		cycleAndWait(h)
		cycleAndWait(h)
		if be.callCount() != 2 {
			t.Fatalf("calls = %d, want 2 with the gap off", be.callCount())
		}
	})
}

// NFR: NFR-012
// UC: UC-040 A2
func TestRemote_TimeoutAndFailureCountAsAnswer(t *testing.T) {
	t.Run("UC-040/A2 a timeout starts the gap at the moment it fires", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		be.block = true
		h, clk, _ := cadenceHarness(t, time.Second, be)
		// A short real timeout; the loop clock moves 3 s while the call runs.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Claude.Timeout = 30 * time.Millisecond })
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		<-be.started
		clk.add(3 * time.Second)
		h.loop.AwaitInflight()
		if g := strAttr(t, spansByName(h.sr.Ended(), SpanDecide)[0], AttrDecideFallbackReason); g != DecideFallbackTimeout {
			t.Fatalf("fallback_reason = %q, want timeout", g)
		}
		clk.add(500 * time.Millisecond) // timeout + 0.5 s
		cycleAndWait(h)
		if be.callCount() != 1 {
			t.Fatalf("calls = %d, want 1: a call started 0.5 s after a timeout (gap 1 s)", be.callCount())
		}
		clk.add(600 * time.Millisecond)
		cycleAndWait(h) // times out again after 30 ms: only the call count matters
		if be.callCount() != 2 {
			t.Fatalf("calls = %d, want 2 once timeout + gap has passed", be.callCount())
		}
	})
	t.Run("UC-040/A2 a failed call starts the gap like an answer", func(t *testing.T) {
		var clk *lockedClock
		be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
			clk.add(2 * time.Second)
			return decide.Response{}, errors.New("upstream 503")
		})
		h, c, _ := cadenceHarness(t, time.Second, be)
		clk = c
		cycleAndWait(h)
		if g := strAttr(t, spansByName(h.sr.Ended(), SpanDecide)[0], AttrDecideFallbackReason); g != DecideFallbackError {
			t.Fatalf("fallback_reason = %q, want error", g)
		}
		clk.add(500 * time.Millisecond)
		cycleAndWait(h)
		if be.callCount() != 1 {
			t.Fatalf("calls = %d, want 1: a call started 0.5 s after a failure (gap 1 s)", be.callCount())
		}
		clk.add(600 * time.Millisecond)
		cycleAndWait(h)
		if be.callCount() != 2 {
			t.Fatalf("calls = %d, want 2 once failure + gap has passed", be.callCount())
		}
	})
}

// overlapBackend answers at once but records the most calls it ever saw in
// flight together.
type overlapBackend struct {
	inFlight atomic.Int32
	maxSeen  atomic.Int32
	calls    atomic.Int32
}

func (b *overlapBackend) Name() string    { return "claude" }
func (b *overlapBackend) Available() bool { return true }
func (b *overlapBackend) Decide(context.Context, decide.Request) (decide.Response, error) {
	n := b.inFlight.Add(1)
	defer b.inFlight.Add(-1)
	for {
		m := b.maxSeen.Load()
		if n <= m || b.maxSeen.CompareAndSwap(m, n) {
			break
		}
	}
	b.calls.Add(1)
	time.Sleep(time.Millisecond) // widen the window a second call could overlap in
	return ffaAnswer("noop", 0.9, "none", 0, "")(decide.Request{})
}

// NFR: NFR-012
// UC: UC-040 A4
func TestRemote_NeverTwoCallsInFlight(t *testing.T) {
	t.Run("UC-040/A4 concurrent cycles never overlap two remote calls of one game", func(t *testing.T) {
		be := &overlapBackend{}
		snap := remoteSnapshot(time.Hour)
		snap.Decision.MinInterval = 0
		snap.Decision.MinGapAfterAnswer = 0 // only the in-flight guard holds calls back
		h := newRemoteHarness(t, snap, nil)
		reg := NewDecideRegistry()
		reg.Register("claude", func(flags.DecisionBackend) decide.Backend { return be })
		h.loop.SetDecideRegistry(reg)
		var wg sync.WaitGroup
		for w := 0; w < 8; w++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for i := 0; i < 50; i++ {
					h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
				}
			}()
		}
		wg.Wait()
		h.loop.AwaitInflight()
		if be.calls.Load() == 0 {
			t.Fatal("no remote call was made")
		}
		if m := be.maxSeen.Load(); m != 1 {
			t.Fatalf("max calls in flight = %d, want 1", m)
		}
	})
}

// discardedByReason reads agent_decide_discarded_total{reason}.
func discardedByReason(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_decide_discarded_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				reason, _ := dp.Attributes.Value("reason")
				out[reason.AsString()] = dp.Value
			}
		}
	}
	return out
}

// UC: UC-040 A7
func TestRemote_StaleAnswersDiscardedAndCounted(t *testing.T) {
	act := ffaAnswer("grant_shield", 0.95, "A", 0.5, "x")
	for _, tc := range []struct {
		name   string
		reason string
		mutate func(h remoteHarness)
	}{
		{"UC-040/A7 target died while the call was in flight", DiscardTargetGone, func(h remoteHarness) {
			c := remoteContext()
			c.Players["A"].Active = boolPtr(false)
			h.provider.set(remoteGameID, c)
		}},
		{"UC-040/A7 game ended while the call was in flight", DiscardGameEnded, func(h remoteHarness) {
			c := remoteContext()
			c.Session.GameActive = boolPtr(false)
			h.provider.set(remoteGameID, c)
		}},
		{"UC-040/A7 game partition evicted while the call was in flight", DiscardStaleContext, func(h remoteHarness) {
			h.provider.evict(remoteGameID)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(act)
			reader := metric.NewManualReader()
			h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
				h.loop.decideDiscarded = newDecideDiscardedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
				tc.mutate(h)
			})
			assertDiscarded(t, h, span, tc.reason)
			if got := discardedByReason(t, reader); got[tc.reason] != 1 || len(got) != 1 {
				t.Fatalf("agent_decide_discarded_total = %v, want {%s: 1}", got, tc.reason)
			}
		})
	}

	// stateCase fires on a context whose state last changed at t0 and lands the
	// answer `took` later on the loop clock.
	stateCase := func(t *testing.T, maxAge, took time.Duration) (remoteHarness, *metric.ManualReader) {
		t.Helper()
		clk := &lockedClock{t: time.Unix(40_000, 0)}
		be := newFakeDecideBackend(func(req decide.Request) (decide.Response, error) {
			clk.add(took)
			return act(req)
		})
		snap := remoteSnapshot(time.Hour)
		snap.Decision.MaxStateAge = maxAge
		h := newRemoteHarness(t, snap, be)
		h.loop.now = clk.now
		reader := metric.NewManualReader()
		h.loop.decideDiscarded = newDecideDiscardedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		c := remoteContext()
		c.UpdatedAt = clk.now()
		h.loop.OnEvaluate(context.Background(), c, testTrigger())
		h.loop.AwaitInflight()
		return h, reader
	}
	t.Run("UC-040/A7 answer built on a state older than decision.max_state_age_ms", func(t *testing.T) {
		h, reader := stateCase(t, 2*time.Second, 3*time.Second)
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		assertDiscarded(t, h, ds[0], DiscardStateTooOld)
		if got := discardedByReason(t, reader); got[DiscardStateTooOld] != 1 || len(got) != 1 {
			t.Fatalf("agent_decide_discarded_total = %v, want {state_too_old: 1}", got)
		}
	})
	t.Run("UC-040/A7 answer inside decision.max_state_age_ms acts", func(t *testing.T) {
		h, reader := stateCase(t, 2*time.Second, time.Second)
		if got := h.sink.decisions(); len(got) != 1 {
			t.Fatalf("applied %d decisions, want 1", len(got))
		}
		if got := discardedByReason(t, reader); len(got) != 0 {
			t.Fatalf("agent_decide_discarded_total = %v, want none", got)
		}
	})
	t.Run("UC-040/A7 max_state_age 0 disables the check", func(t *testing.T) {
		h, _ := stateCase(t, 0, time.Hour)
		if got := h.sink.decisions(); len(got) != 1 {
			t.Fatalf("applied %d decisions, want 1", len(got))
		}
	})
}
