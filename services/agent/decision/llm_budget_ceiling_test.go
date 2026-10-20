package decision

import (
	"io"
	"log/slog"
	"testing"
)

// llm_budget_ceiling_test.go covers the #1788 remote-call ceiling (NFR-011, FR-152,
// C-034): the enforced cap is min(AGENT_LLM_MAX_REQUESTS_PER_MINUTE,
// llm.max_requests_per_minute); an unset env is no ceiling; the flag can only
// tighten, never loosen. The flag's lost-sync value (6) comes from flags.intFlag.

// NFR: NFR-011
func TestCeilBudget(t *testing.T) {
	cases := []struct {
		name                string
		ceiling, flag, want int
	}{
		{"unset env: the flag governs", 0, 30, 30},
		{"flag above the ceiling is capped", 12, 30, 12},
		{"flag below the ceiling tightens", 12, 3, 3},
		{"equal", 6, 6, 6},
		{"lost sync: flag default 6 under a higher ceiling", 20, 6, 6},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ceilBudget(tc.ceiling, tc.flag); got != tc.want {
				t.Fatalf("ceilBudget(%d, %d) = %d, want %d", tc.ceiling, tc.flag, got, tc.want)
			}
		})
	}
}

// The decide path (Loop) applies the same rule as the retro and the proposer.
// NFR: NFR-011
func TestLoop_BudgetCeilingCapsFlag(t *testing.T) {
	l := NewLoop(fakeFlags{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if got := l.budgetCap(30); got != 30 {
		t.Fatalf("no ceiling: budgetCap(30) = %d, want 30", got)
	}
	l.SetLLMBudgetCeiling(4)
	if got := l.budgetCap(30); got != 4 {
		t.Fatalf("ceiling 4, flag 30: budgetCap = %d, want 4 (the flag cannot loosen the ceiling)", got)
	}
	if got := l.budgetCap(2); got != 2 {
		t.Fatalf("ceiling 4, flag 2: budgetCap = %d, want 2 (the flag tightens)", got)
	}
}
