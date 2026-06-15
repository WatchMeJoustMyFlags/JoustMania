package decision

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// llm_budget_override_test.go covers the #964 LLM budget override: an operator
// startup knob (AGENT_LLM_MAX_REQUESTS_PER_MINUTE, wired in main.go via
// SetLLMBudgetOverride) that REPLACES the live llm.max_requests_per_minute flag value
// as the per-minute cap, so a real-inference dry-run is not gated by
// llm_budget_exhausted under the eval-cycle rate. A non-positive override is ignored
// (the flag governs) — the default-off path is unchanged.

// TestBudgetOverride_RaisesCapAboveFlag: with a tiny flag budget (2) but an override
// of 5, five attempts are admitted before the budget exhausts — the override governs,
// not the flag. This is the live-inference fix: the loop reaches mode=llm instead of
// gating after 2 cycles.
func TestBudgetOverride_RaisesCapAboveFlag(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  0, // no cadence floor — isolate the budget layer
		MaxRequestsPerMinute: 2, // the flag says only 2/min
	})
	l, _, reader := gateLoop(t, snap, NewLLMBudget(), &clock)
	l.SetLLMBudgetOverride(5) // operator override: 5/min
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	for i := 0; i < 5; i++ {
		l.OnEvaluate(context.Background(), ctx, testTrigger())
	}
	// Five admitted (override cap), none gated yet.
	if got := gatedByReason(t, reader); got[FallbackBudgetExhausted] != 0 {
		t.Errorf("after 5 cycles under override=5, budget-exhausted gated = %d, want 0", got[FallbackBudgetExhausted])
	}
	// The sixth exceeds the override cap.
	l.OnEvaluate(context.Background(), ctx, testTrigger())
	if got := gatedByReason(t, reader); got[FallbackBudgetExhausted] != 1 {
		t.Errorf("6th cycle: budget-exhausted gated = %d, want 1 (override cap of 5 reached)", got[FallbackBudgetExhausted])
	}
}

// TestBudgetOverride_ZeroIgnored_FlagGoverns: a non-positive override is ignored, so
// the flag value (2) governs exactly as before — the default-off regression guard.
func TestBudgetOverride_ZeroIgnored_FlagGoverns(t *testing.T) {
	clock := time.Unix(1000, 0)
	snap := gateSnapshot(flags.LLMGate{
		EligibleGameKinds:    []string{"real"},
		MinDecisionInterval:  0,
		MaxRequestsPerMinute: 2,
	})
	l, _, reader := gateLoop(t, snap, NewLLMBudget(), &clock)
	l.SetLLMBudgetOverride(0)  // ignored
	l.SetLLMBudgetOverride(-3) // ignored
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 1 admitted
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 2 admitted
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // 3 -> exhausted (flag cap of 2)

	if got := gatedByReason(t, reader); got[FallbackBudgetExhausted] != 1 {
		t.Errorf("budget-exhausted gated = %d, want 1 (flag cap of 2 governs when override unset)", got[FallbackBudgetExhausted])
	}
}

// TestBudgetCap_Unit: the cap selection helper directly — override wins when > 0, the
// flag value governs otherwise.
func TestBudgetCap_Unit(t *testing.T) {
	l := &Loop{}
	if got := l.budgetCap(6); got != 6 {
		t.Errorf("no override: budgetCap(6) = %d, want 6 (flag governs)", got)
	}
	l.budgetOverride = 20
	if got := l.budgetCap(6); got != 20 {
		t.Errorf("override=20: budgetCap(6) = %d, want 20", got)
	}
}
