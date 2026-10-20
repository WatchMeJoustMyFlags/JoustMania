package decision

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// uc040_gaps_test.go closes UC-040 clauses the per-feature suites assert only in
// part (#1725): A3's order (configuration fallbacks ahead of the allow-list), A22
// (the agent's spans read act whatever the coordinator does), A23 within a rules
// fallback, and A29 for an answer that lands after the cancellation.

// lateAnswerBackend ignores its context: it answers only once released, so its
// answer lands after the shutdown cancelled the call.
type lateAnswerBackend struct {
	*fakeDecideBackend
}

func (b lateAnswerBackend) Decide(_ context.Context, req decide.Request) (decide.Response, error) {
	b.mu.Lock()
	b.calls++
	b.mu.Unlock()
	close(b.started)
	<-b.release
	return b.answer(req)
}

// UC: UC-040 A29
func TestRemote_AnswerLandingAfterShutdownIsDropped(t *testing.T) {
	t.Run("UC-040/A29 an act answer that lands after the cancellation is dropped as shutdown", func(t *testing.T) {
		be := lateAnswerBackend{newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))}
		h := newRemoteHarness(t, remoteSnapshot(time.Hour), be)
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		<-be.started
		h.cancel() // shutdown while the call is in flight
		close(be.release)
		h.loop.AwaitInflight()

		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		assertDiscarded(t, h, ds[0], decideDiscardShutdown)
		if g := strAttr(t, ds[0], AttrDecideFallbackReason); g != "" {
			t.Fatalf("fallback_reason = %q, want no rules fallback", g)
		}
	})

	t.Run("UC-040/A29 a backend that returns ctx.Err() on the cancellation is dropped as shutdown", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) { h.cancel() })
		assertDiscarded(t, h, span, decideDiscardShutdown)
	})
}

// UC: UC-040 A3 A10
func TestRemote_ConfigurationFallbackRunsBeforeTheAllowList(t *testing.T) {
	unavailable := newFakeDecideBackend(nil)
	unavailable.available = false
	for _, tc := range []struct {
		name   string
		be     decide.Backend
		reason string
	}{
		{"UC-040/A3 unknown backend falls back to rules with an empty allow-list", nil, DecideFallbackUnknownBackend},
		{"UC-040/A3 unavailable backend falls back to rules with an empty allow-list", unavailable, DecideFallbackUnavailable},
	} {
		t.Run(tc.name, func(t *testing.T) {
			snap := remoteSnapshot(time.Second)
			snap.InterventionsAllowed = nil
			h := newRemoteHarness(t, snap, tc.be)
			state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())

			ds := spansByName(h.sr.Ended(), SpanDecide)
			if len(ds) != 1 {
				t.Fatalf("agent.decide spans = %d, want 1", len(ds))
			}
			if g := strAttr(t, ds[0], AttrDecideFallbackReason); g != tc.reason {
				t.Fatalf("fallback_reason = %q, want %q (not the allow-list gate)", g, tc.reason)
			}
			// Rules decided; the allow-list then blocks their decisions (A10).
			if len(state.Outcomes) == 0 {
				t.Fatal("rules must decide when the backend cannot be called, even with an empty allow-list")
			}
			for _, o := range state.Outcomes {
				if o.Dispatched || o.BlockReason != ReasonNotAllowed {
					t.Fatalf("outcome %+v, want blocked not_allowed", o)
				}
			}
			if got := h.sink.decisions(); len(got) != 0 {
				t.Fatalf("applied %+v, want nothing", got)
			}
		})
	}
}

// UC: UC-040 A22
func TestRemote_SpansReadActWhateverTheCoordinatorDoes(t *testing.T) {
	t.Run("UC-040/A22 a dispatched act is recorded as act and not blocked; no coordinator verdict feeds back", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "x"))
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		span := h.run(t)

		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
			t.Fatalf("decide.outcome = %q, want act", g)
		}
		if v, ok := attrValue(span, AttrDecideBlocked); !ok || v.AsBool() {
			t.Fatalf("decide.blocked = %v (set=%v), want false", v.AsBool(), ok)
		}
		dec := spansByName(h.sr.Ended(), SpanDecision)
		if len(dec) != 1 {
			t.Fatalf("agent.decision spans = %d, want 1", len(dec))
		}
		if v, ok := attrValue(dec[0], AttrDecisionBlocked); !ok || v.AsBool() {
			t.Fatalf("decision.blocked = %v (set=%v), want false", v.AsBool(), ok)
		}
		// The spans have ended when the write returns: the coordinator's later
		// re-check (step 13) cannot change them.
		if !span.EndTime().After(span.StartTime()) || dec[0].EndTime().IsZero() {
			t.Fatal("the decide and decision spans must have ended at dispatch")
		}
	})
}

// UC: UC-040 A23 A2 A3
func TestRemote_ProbeDecidesInARulesFallback(t *testing.T) {
	t.Run("UC-040/A23 A2 a remote error falls back to the probe engine, which emits its noop", func(t *testing.T) {
		be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
			return decide.Response{}, errors.New("boom")
		})
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		h.loop.Rules = NewProbeRules(5*time.Second, nil)
		span := h.run(t)

		if g := strAttr(t, span, AttrDecideFallbackReason); g != DecideFallbackError {
			t.Fatalf("fallback_reason = %q, want error", g)
		}
		got := h.sink.decisions()
		if len(got) != 1 || got[0].Intervention != InterventionNoop {
			t.Fatalf("applied %+v, want the probe's one noop", got)
		}
	})

	t.Run("UC-040/A23 A3 an unavailable backend falls back to the probe engine in the same cycle", func(t *testing.T) {
		be := newFakeDecideBackend(nil)
		be.available = false
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		h.loop.Rules = NewProbeRules(5*time.Second, nil)
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())

		if len(state.Outcomes) != 1 || state.Outcomes[0].Intervention != InterventionNoop || !state.Outcomes[0].Dispatched {
			t.Fatalf("outcomes = %+v, want the probe's dispatched noop", state.Outcomes)
		}
		if be.callCount() != 0 {
			t.Fatal("an unavailable backend must not be called")
		}
	})
}
