package main

import "testing"

// llm_budget_override_main_test.go covers the #964 AGENT_LLM_MAX_REQUESTS_PER_MINUTE
// env parsing in main: a positive value overrides the flag-derived budget cap; an
// unset/empty/invalid/non-positive value returns 0 ("no override", the flag governs),
// keeping the default-off path unchanged.
func TestLLMBudgetOverride(t *testing.T) {
	tests := []struct {
		name string
		set  bool
		val  string
		want int
	}{
		{"unset -> no override", false, "", 0},
		{"empty -> no override", true, "", 0},
		{"positive -> override", true, "20", 20},
		{"zero -> no override", true, "0", 0},
		{"negative -> no override", true, "-5", 0},
		{"garbage -> no override", true, "lots", 0},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if tc.set {
				t.Setenv("AGENT_LLM_MAX_REQUESTS_PER_MINUTE", tc.val)
			} else {
				// Ensure the var is unset for this case (t.Setenv restores afterward,
				// but a prior process env could leak in — explicitly clear it).
				t.Setenv("AGENT_LLM_MAX_REQUESTS_PER_MINUTE", "")
			}
			if got := llmBudgetOverride(); got != tc.want {
				t.Errorf("llmBudgetOverride() = %d, want %d", got, tc.want)
			}
		})
	}
}
