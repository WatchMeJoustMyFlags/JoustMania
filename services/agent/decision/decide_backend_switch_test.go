package decision

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
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

// UC-044 A2, model half: the operator changes only decision.claude.model while a
// claude call is in flight. The backend is unchanged, so the late answer is NOT
// discarded (no backend_changed): it is routed and applied, and its span keeps
// the model it was sent with. The next call is built with the new model.
//
// UC: UC-044 A2
func TestRemote_ModelOnlyFlipKeepsInflightAnswer(t *testing.T) {
	const oldModel, newModel = "claude-haiku-4-5-20251001", "claude-sonnet-4-5"
	be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
	var (
		mu         sync.Mutex
		seenModels []string
	)
	h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
		// Record the model each later call is built with (the registry hands the
		// factory the flags of the firing cycle).
		reg := NewDecideRegistry()
		reg.Register(be.Name(), func(cfg flags.DecisionBackend) decide.Backend {
			mu.Lock()
			seenModels = append(seenModels, cfg.Claude.Model)
			mu.Unlock()
			return be
		})
		h.loop.SetDecideRegistry(reg)
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Claude.Model = newModel })
	})

	t.Run("UC-044/A2 a model-only flip leaves the in-flight answer alone", func(t *testing.T) {
		for key, want := range map[string]string{
			AttrDecideOutcome:          DecideOutcomeAct,
			AttrDecideBackend:          flags.DecisionBackendClaude,
			AttrDecideBackendRequested: flags.DecisionBackendClaude,
			"gen_ai.request.model":     oldModel,
		} {
			if got := strAttr(t, span, key); got != want {
				t.Errorf("in-flight %s = %q, want %q", key, got, want)
			}
		}
		if _, ok := attrValue(span, AttrDecideDiscardedReason); ok {
			t.Errorf("in-flight answer carries %s; a model-only flip must not discard it", AttrDecideDiscardedReason)
		}
		if got := h.sink.decisions(); len(got) != 1 || got[0].Intervention != "grant_shield" {
			t.Fatalf("applied %+v, want the in-flight grant_shield", got)
		}
	})

	t.Run("UC-044/A2 the next call uses the new model", func(t *testing.T) {
		advance(h.clock, time.Minute) // past decision.min_interval_ms
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 2 {
			t.Fatalf("agent.decide spans = %d, want 2", len(ds))
		}
		if got := strAttr(t, ds[1], "gen_ai.request.model"); got != newModel {
			t.Errorf("next call gen_ai.request.model = %q, want %q", got, newModel)
		}
		mu.Lock()
		defer mu.Unlock()
		if len(seenModels) != 1 || seenModels[0] != newModel {
			t.Fatalf("backend built with models %v, want [%s]", seenModels, newModel)
		}
	})
}
