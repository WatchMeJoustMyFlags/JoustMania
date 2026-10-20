package decision

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// newDecideSpans returns the agent.decide spans ended since the first `seen`.
func newDecideSpans(h remoteHarness, seen int) ([]sdktrace.ReadOnlySpan, int) {
	all := spansByName(h.sr.Ended(), SpanDecide)
	return all[seen:], len(all)
}

// UC-044 main steps 4-5: the decision backend is read from the flags every
// cycle, never cached, so a flip of decision.backend applies to the next
// decision without a restart, and the agent.decide span names the new value.
//
// UC: UC-044 main Post-S-1
func TestRemote_BackendSwitchAppliesNextCycle(t *testing.T) {
	t.Run("UC-044/main rules -> claude -> rules applies on the next cycle each time", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "switch"))
		snap := remoteSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendRules
		h := newRemoteHarness(t, snap, be)
		// Dispatched decisions schedule an effect sampler that AwaitInflight joins;
		// keep its follow-up window short so each cycle settles quickly.
		h.loop.SetEffectWindow(time.Millisecond)
		ctx := context.Background()

		// Cycle 1 on rules: the rules engine decides in the same cycle; no remote call.
		h.loop.OnEvaluate(ctx, remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		spans, seen := newDecideSpans(h, 0)
		if len(spans) != 1 {
			t.Fatalf("cycle 1: agent.decide spans = %d, want 1", len(spans))
		}
		if got := strAttr(t, spans[0], AttrDecideBackend); got != "rules" {
			t.Fatalf("cycle 1: decide.backend = %q, want rules", got)
		}
		if n := be.callCount(); n != 0 {
			t.Fatalf("cycle 1: remote calls = %d, want 0 on rules", n)
		}

		// Operator flips decision.backend to claude (agent-backend.sh claude).
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendClaude })
		advance(h.clock, 2*time.Second)

		state := h.loop.OnEvaluate(ctx, remoteContext(), testTrigger())
		if len(state.Outcomes) != 0 {
			t.Fatalf("cycle 2 decided synchronously %+v; want the remote path", state.Outcomes)
		}
		h.loop.AwaitInflight()
		spans, seen = newDecideSpans(h, seen)
		if len(spans) != 1 {
			t.Fatalf("cycle 2: agent.decide spans = %d, want 1", len(spans))
		}
		if n := be.callCount(); n != 1 {
			t.Fatalf("cycle 2: remote calls = %d, want 1 after the flip", n)
		}
		for key, want := range map[string]string{
			AttrDecideBackendRequested: "claude",
			AttrDecideBackend:          "claude",
			AttrDecideOutcome:          DecideOutcomeAct,
		} {
			if got := strAttr(t, spans[0], key); got != want {
				t.Fatalf("cycle 2: %s = %q, want %q", key, got, want)
			}
		}

		// Flip back to rules: the next cycle makes no remote call.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendRules })
		advance(h.clock, 2*time.Second)
		h.loop.OnEvaluate(ctx, remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		if n := be.callCount(); n != 1 {
			t.Fatalf("cycle 3: remote calls = %d, want still 1 after switching back to rules", n)
		}
		spans, _ = newDecideSpans(h, seen)
		if len(spans) != 1 {
			t.Fatalf("cycle 3: agent.decide spans = %d, want 1 (rules decides again)", len(spans))
		}
		for _, s := range spans {
			if got := strAttr(t, s, AttrDecideBackendRequested); got != "rules" {
				t.Fatalf("cycle 3: decide.backend.requested = %q, want rules", got)
			}
		}
	})
}
