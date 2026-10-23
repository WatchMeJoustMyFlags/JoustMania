package decision

import (
	"context"
	"log/slog"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// decide_fallback_quiet_test.go: a same-cycle fallback (the requested backend
// could not be called, so rules decide) must be observable even when the rules
// engine then emits nothing. Before the fix such a cycle returned before any
// span, and unknown_backend / backend_unavailable / async_not_wired had no metric
// either, so a missing JEV_API_KEY was invisible while rules were quiet.

// quietContext is a live real game in which no rule fires (early, nobody out).
func quietContext() gamecontext.GameContext {
	c := gameCtx(2, nil, map[string]testPlayer{"A": {active: true}, "B": {active: true}})
	c.GameKind = "real"
	return c
}

// quietFallbackHarness is a remote harness on the loop's fake clock with an
// observable gated counter and log.
func quietFallbackHarness(t *testing.T, be decide.Backend) (remoteHarness, *metric.ManualReader, *syncBuffer) {
	t.Helper()
	h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
	h.loop.now = func() time.Time { return *h.clock }
	h.loop.SetThrottle(5 * time.Second)
	reader := metric.NewManualReader()
	h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
	logs := &syncBuffer{}
	h.loop.Log = slog.New(slog.NewTextHandler(logs, nil))
	return h, reader, logs
}

// UC: UC-040 A3
func TestRemote_SameCycleFallbackObservableWhenRulesQuiet(t *testing.T) {
	unavailable := newFakeDecideBackend(nil)
	unavailable.available = false
	cases := []struct {
		name     string
		be       decide.Backend
		unwire   bool
		reason   string
		errorLog bool
	}{
		{"unknown_backend", nil, false, DecideFallbackUnknownBackend, true},
		{"backend_unavailable", unavailable, false, DecideFallbackUnavailable, true},
		{"async_not_wired", newFakeDecideBackend(nil), true, DecideFallbackNotWired, true},
	}
	for _, tc := range cases {
		t.Run("UC-040/A3 "+tc.name+" records outcome=fallback while rules stay quiet", func(t *testing.T) {
			h, reader, logs := quietFallbackHarness(t, tc.be)
			if tc.unwire {
				h.loop.SetContextProvider(nil)
			}

			// Three cycles 1s apart (the rules engine ranks once per second): all
			// inside one 5s throttle window.
			const cycles = 3
			for i := 0; i < cycles; i++ {
				state := h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger())
				if len(state.Outcomes) != 0 {
					t.Fatalf("cycle %d: rules decided %+v; the test needs a quiet context", i, state.Outcomes)
				}
				advance(h.clock, time.Second)
			}

			ds := spansByName(h.sr.Ended(), SpanDecide)
			if len(ds) != 1 {
				t.Fatalf("agent.decide spans = %d, want 1 (throttled to one per window)", len(ds))
			}
			if g := strAttr(t, ds[0], AttrDecideOutcome); g != DecideOutcomeFallback {
				t.Fatalf("outcome = %q, want fallback", g)
			}
			if g := strAttr(t, ds[0], AttrDecideFallbackReason); g != tc.reason {
				t.Fatalf("fallback_reason = %q, want %q", g, tc.reason)
			}
			if g := strAttr(t, ds[0], AttrDecideBackend); g != RulesBackendName {
				t.Fatalf("decide.backend = %q, want rules", g)
			}
			if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
				t.Fatalf("agent.decision spans = %d, want 0 (nothing was decided)", n)
			}
			if g := gatedByReason(t, reader)[tc.reason]; g != cycles {
				t.Fatalf("agent_llm_gated_total{reason=%s} = %d, want %d (every cycle counted)", tc.reason, g, cycles)
			}
			if n := logs.count("level=WARN msg=agent.decide.backend_misconfigured"); n != 1 {
				t.Fatalf("misconfigured warnings = %d, want 1 (on entering the state, not per cycle)", n)
			}

			// After the throttle window the fallback is recorded again.
			advance(h.clock, 5*time.Second)
			h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger())
			if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != 2 {
				t.Fatalf("agent.decide spans after the window = %d, want 2", n)
			}
			if n := logs.count("level=WARN msg=agent.decide.backend_misconfigured"); n != 1 {
				t.Fatalf("misconfigured warnings = %d, want still 1 (no new transition)", n)
			}
		})
	}
}

// The decided-cycle fallback span (rules emitted a decision) is unchanged and not
// throttled: every decided cycle still carries its own agent.decide span.
// UC: UC-040 A3
func TestRemote_SameCycleFallbackDecidedCycleUnthrottled(t *testing.T) {
	h, reader, _ := quietFallbackHarness(t, nil)
	for i := 0; i < 2; i++ {
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		if len(state.Outcomes) == 0 {
			t.Fatalf("cycle %d: rules must decide on the remote context", i)
		}
		advance(h.clock, 2*time.Second)
	}
	if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != 2 {
		t.Fatalf("agent.decide spans = %d, want 2 (one per decided cycle)", n)
	}
	if g := gatedByReason(t, reader)[DecideFallbackUnknownBackend]; g != 2 {
		t.Fatalf("agent_llm_gated_total{reason=unknown_backend} = %d, want 2", g)
	}
}

// The default rules path is untouched: a quiet rules cycle emits no span, no
// gated metric and no fallback warning.
// UC: UC-040 A1
func TestRules_QuietCycleStaysSpanFree(t *testing.T) {
	h, reader, logs := quietFallbackHarness(t, nil)
	h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendRules })
	h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger())
	if n := len(decisionTraceSpans(h.sr.Ended())); n != 0 {
		t.Fatalf("spans = %d, want 0 on a quiet rules cycle", n)
	}
	if g := gatedByReason(t, reader); len(g) != 0 {
		t.Fatalf("gated = %v, want none", g)
	}
	if n := logs.count("agent.decide.backend_misconfigured"); n != 0 {
		t.Fatalf("misconfigured logs = %d, want 0", n)
	}
}

// llm_not_eligible and llm_budget_exhausted are normal operation (shadow games,
// spend cap): on a quiet cycle they stay counter-only, no span, no warning.
// UC: UC-040 A3
func TestRemote_QuietGateFallbackIsCounterOnly(t *testing.T) {
	t.Run("UC-040/A3 llm_not_eligible on a quiet shadow game", func(t *testing.T) {
		h, reader, logs := quietFallbackHarness(t, newFakeDecideBackend(nil))
		c := quietContext()
		c.GameKind = "shadow"
		for i := 0; i < 3; i++ {
			h.loop.OnEvaluate(context.Background(), c, testTrigger())
			advance(h.clock, 6*time.Second)
		}
		if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != 0 {
			t.Fatalf("agent.decide spans = %d, want 0", n)
		}
		if g := gatedByReason(t, reader)[FallbackNotEligible]; g != 3 {
			t.Fatalf("gated[%s] = %d, want 3", FallbackNotEligible, g)
		}
		if n := logs.count("agent.decide.backend_misconfigured"); n != 0 {
			t.Fatalf("misconfigured logs = %d, want 0", n)
		}
	})
}

// Each configuration reason has its own throttle window, so one reason does not
// hide another; the WARN fires on each change of reason and an INFO when the
// configuration is fixed.
// UC: UC-040 A3
func TestRemote_QuietConfigFallbackPerReasonAndTransitions(t *testing.T) {
	be := newFakeDecideBackend(nil)
	be.available = false
	h, _, logs := quietFallbackHarness(t, be)

	h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger()) // backend_unavailable
	advance(h.clock, time.Second)
	be.available = true
	h.loop.SetContextProvider(nil)
	h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger()) // async_not_wired, same window

	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) != 2 {
		t.Fatalf("agent.decide spans = %d, want 2 (one per reason inside one window)", len(ds))
	}
	if a, b := strAttr(t, ds[0], AttrDecideFallbackReason), strAttr(t, ds[1], AttrDecideFallbackReason); a != DecideFallbackUnavailable || b != DecideFallbackNotWired {
		t.Fatalf("reasons = %q, %q; want backend_unavailable, async_not_wired", a, b)
	}
	if n := logs.count("level=WARN msg=agent.decide.backend_misconfigured"); n != 2 {
		t.Fatalf("misconfigured warnings = %d, want 2 (one per reason change)", n)
	}

	// Configuration fixed: back to rules. One INFO, no further warnings.
	h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendRules })
	advance(h.clock, time.Second)
	h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger())
	advance(h.clock, time.Second)
	h.loop.OnEvaluate(context.Background(), quietContext(), testTrigger())
	if n := logs.count("level=INFO msg=agent.decide.backend_misconfigured"); n != 1 {
		t.Fatalf("cleared logs = %d, want 1", n)
	}
	if n := logs.count("level=WARN msg=agent.decide.backend_misconfigured"); n != 2 {
		t.Fatalf("misconfigured warnings = %d, want still 2", n)
	}
}
