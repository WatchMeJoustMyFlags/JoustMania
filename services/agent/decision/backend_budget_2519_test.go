package decision

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/flags"
)

// backend_budget_2519_test.go covers the paid backends' own decide caps (#2519,
// FR-152 (c), NFR-011, C-034): decision.claude.max_requests_per_minute and
// decision.openai.max_requests_per_minute tighten the global
// llm.max_requests_per_minute for decide calls on their backend. One window, one
// critical section: entries are tagged by backend; jev, the retro and the
// proposer stay on the global budget only.

// TestLLMBudget_BackendCapTightens: global 10, claude cap 2. The third claude
// charge is refused while global capacity is left; jev and untagged (retro,
// proposer) charges are admitted until the total reaches 10, and the untagged ones
// count toward the total but not toward claude.
// NFR: NFR-011
func TestLLMBudget_BackendCapTightens(t *testing.T) {
	b := NewLLMBudget()
	now := time.Unix(1000, 0)
	const global, claudeCap = 10, 2
	for i := 0; i < claudeCap; i++ {
		if !b.allowBackend(now, global, flags.DecisionBackendClaude, claudeCap) {
			t.Fatalf("claude charge %d refused under cap %d", i+1, claudeCap)
		}
	}
	if b.allowBackend(now, global, flags.DecisionBackendClaude, claudeCap) {
		t.Fatal("third claude charge admitted, want refused by the claude cap 2")
	}
	// Untagged charges (the retro, the proposer) fill the total but not claude.
	for i := 0; i < 3; i++ {
		if !b.allow(now, global) {
			t.Fatalf("untagged charge %d refused", i+1)
		}
	}
	// jev has no cap of its own: admitted until the global total is reached.
	for i := 0; i < global-claudeCap-3; i++ {
		if !b.allowBackend(now, global, flags.DecisionBackendJev, -1) {
			t.Fatalf("jev charge %d refused before the global total %d", i+1, global)
		}
	}
	if b.allowBackend(now, global, flags.DecisionBackendJev, -1) {
		t.Fatal("jev charge admitted above the global total")
	}
	if got := b.countBackend(flags.DecisionBackendClaude); got != claudeCap {
		t.Fatalf("claude entries = %d, want %d (untagged charges never count toward claude)", got, claudeCap)
	}
	// The window slides for the backend count as for the total.
	later := now.Add(llmBudgetWindow + time.Second)
	if !b.allowBackend(later, global, flags.DecisionBackendClaude, claudeCap) {
		t.Fatal("claude charge after the window slid refused")
	}
	// A zero backend cap closes the backend even with global capacity left.
	if b.allowBackend(later, global, flags.DecisionBackendOpenAI, 0) {
		t.Fatal("openai cap 0 admitted, want closed")
	}
}

// backendCapSnapshot is a claude remote snapshot with no cadence (so the gate
// admits back to back), the given global budget and claude cap.
func backendCapSnapshot(global int, claudeCap int) flags.Snapshot {
	s := remoteSnapshot(time.Second)
	s.LLMGate.MaxRequestsPerMinute = global
	s.Decision.MinInterval = 0
	s.Decision.MinGapAfterAnswer = 0
	s.Decision.Claude.MaxRequestsPerMinute = claudeCap
	s.Decision.Claude.HasMaxRequestsPerMinute = true
	return s
}

// TestRemote_BackendCapFallsBackToRules: decision.backend=claude, global 600,
// claude cap 2. Two decide calls are admitted; the third cycle makes no remote
// call, rules decide in the same cycle, the span records llm_budget_exhausted and
// agent_llm_gated_total counts it. The same window on jev admits the third call.
// NFR: NFR-011
// UC: UC-040 A3
func TestRemote_BackendCapFallsBackToRules(t *testing.T) {
	t.Run("UC-040/A3 claude cap 2 under global 600: third call falls back to rules", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		snap := backendCapSnapshot(600, 2)
		h := newRemoteHarness(t, snap, be)
		reader := metric.NewManualReader()
		h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		for i := 0; i < 2; i++ {
			if r := h.loop.gateRemoteDecide(context.Background(), snap, remoteContext()); r != "" {
				t.Fatalf("admission %d gated with %q, want admitted", i+1, r)
			}
		}

		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())

		if h.loop.inflight.Load() {
			t.Fatal("in-flight slot still claimed: a remote call was launched")
		}
		if n := be.callCount(); n != 0 {
			t.Fatalf("backend calls = %d, want 0 (claude cap exhausted)", n)
		}
		if len(state.Outcomes) == 0 {
			t.Fatal("rules must decide in the same cycle when the claude cap is exhausted")
		}
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		for key, w := range map[string]string{
			AttrDecideBackend:          RulesBackendName,
			AttrDecideBackendRequested: flags.DecisionBackendClaude,
			AttrDecideFallbackReason:   FallbackBudgetExhausted,
		} {
			if g := strAttr(t, ds[0], key); g != w {
				t.Errorf("%s = %q, want %q", key, g, w)
			}
		}
		if g := gatedByReason(t, reader)[FallbackBudgetExhausted]; g != 1 {
			t.Fatalf("agent_llm_gated_total{reason=%s} = %d, want 1", FallbackBudgetExhausted, g)
		}
	})
	t.Run("UC-040/A3 jev has no cap of its own: third call admitted", func(t *testing.T) {
		snap := backendCapSnapshot(600, 2)
		snap.Decision.Backend = flags.DecisionBackendJev
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x")))
		for i := 0; i < 3; i++ {
			if r := h.loop.gateRemoteDecide(context.Background(), snap, remoteContext()); r != "" {
				t.Fatalf("jev admission %d gated with %q, want admitted", i+1, r)
			}
		}
	})
}

// TestRemote_BackendCapGate pins the composition of the caps at the decide gate:
// the effective rate is min(env, global, backend).
// NFR: NFR-011
// UC: UC-040 A3
func TestRemote_BackendCapGate(t *testing.T) {
	admitN := func(t *testing.T, h remoteHarness, snap flags.Snapshot, n int) {
		t.Helper()
		for i := 0; i < n; i++ {
			if r := h.loop.gateRemoteDecide(context.Background(), snap, remoteContext()); r != "" {
				t.Fatalf("admission %d gated with %q, want admitted", i+1, r)
			}
		}
	}
	gated := func(t *testing.T, h remoteHarness, snap flags.Snapshot) {
		t.Helper()
		if r := h.loop.gateRemoteDecide(context.Background(), snap, remoteContext()); r != FallbackBudgetExhausted {
			t.Fatalf("gate = %q, want %q", r, FallbackBudgetExhausted)
		}
	}
	t.Run("UC-040/A3 claude cap 2 binds under a high env ceiling 600", func(t *testing.T) {
		snap := backendCapSnapshot(600, 2)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("noop", 0.95, "", 0, "x")))
		h.loop.SetLLMBudgetCeiling(600)
		admitN(t, h, snap, 2)
		gated(t, h, snap)
	})
	// Guard: passes before #2519 too, but kills the "backend cap overrides the
	// global budget" mutation (FR-152 (c): the cap only tightens).
	t.Run("UC-040/A3 global 6 binds under claude cap 30", func(t *testing.T) {
		snap := backendCapSnapshot(6, 30)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("noop", 0.95, "", 0, "x")))
		admitN(t, h, snap, 6)
		gated(t, h, snap)
	})
	// Guard: the retro and the proposer are not charged to the claude cap
	// (decision 4 of #2519): five untagged charges, then a claude decide still fits.
	t.Run("UC-040/A3 retro charges do not count toward the claude cap", func(t *testing.T) {
		snap := backendCapSnapshot(600, 2)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("noop", 0.95, "", 0, "x")))
		now := h.loop.nowOrDefault()()
		for i := 0; i < 5; i++ {
			if !h.loop.budget.allow(now, 600) {
				t.Fatalf("retro charge %d refused", i+1)
			}
		}
		admitN(t, h, snap, 2)
		gated(t, h, snap)
	})
	t.Run("UC-040/A3 a zero claude cap closes claude", func(t *testing.T) {
		snap := backendCapSnapshot(600, 0)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("noop", 0.95, "", 0, "x")))
		gated(t, h, snap)
	})
	// A negative cap that still claims to apply (a hand-built snapshot, or a future
	// resolver change) closes the backend at the gate too: it never reads as
	// "no cap" (fail closed, C-034).
	t.Run("UC-040/A3 a negative claude cap with Has closes claude", func(t *testing.T) {
		snap := backendCapSnapshot(600, -5)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer("noop", 0.95, "", 0, "x")))
		gated(t, h, snap)
	})
}
