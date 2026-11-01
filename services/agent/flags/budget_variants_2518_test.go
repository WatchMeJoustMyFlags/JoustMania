package flags

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

// #2518: operator-only higher variants for a cheap remote backend (Jev):
// llm.max_requests_per_minute `interactive` = 120 and
// policy.max_interventions_per_minute `lively` = 8. The defaults stay 6 and 2,
// and the agent never writes either flag (C-034, agent_set_never_written_c034_test.go).

const (
	budgetVariantInteractive = 120
	budgetVariantLively      = 8
	budgetVariantFrenzyLLM   = 600
	budgetVariantFrenzy      = 20
)

func readAgentFlagFile(t *testing.T, path string) map[string]map[string]any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Flags map[string]map[string]any `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("%s: %v", path, err)
	}
	return doc.Flags
}

func budgetFlagFiles() []string {
	return []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
	}
}

// UC: UC-070 A4 BR-003
// NFR: NFR-011 NFR-015
func TestBudgetVariants2518_DeclaredInBothFlagFiles(t *testing.T) {
	for _, path := range budgetFlagFiles() {
		flags := readAgentFlagFile(t, path)
		variant := func(key, name string) any {
			v, _ := flags[key]["variants"].(map[string]any)
			return v[name]
		}
		def := func(key string) any {
			return variant(key, flags[key]["defaultVariant"].(string))
		}
		t.Run("UC-070/A4 "+path, func(t *testing.T) {
			if got := variant(keyLLMMaxRequestsPerMin, "interactive"); got != float64(budgetVariantInteractive) {
				t.Errorf("%s interactive = %v, want %d", keyLLMMaxRequestsPerMin, got, budgetVariantInteractive)
			}
			if got := variant(keyMaxInterventionsPerMinute, "lively"); got != float64(budgetVariantLively) {
				t.Errorf("%s lively = %v, want %d", keyMaxInterventionsPerMinute, got, budgetVariantLively)
			}
			if got := variant(keyLLMMaxRequestsPerMin, "frenzy"); got != float64(budgetVariantFrenzyLLM) {
				t.Errorf("%s frenzy = %v, want %d", keyLLMMaxRequestsPerMin, got, budgetVariantFrenzyLLM)
			}
			if got := variant(keyMaxInterventionsPerMinute, "frenzy"); got != float64(budgetVariantFrenzy) {
				t.Errorf("%s frenzy = %v, want %d", keyMaxInterventionsPerMinute, got, budgetVariantFrenzy)
			}
			if got := def(keyLLMMaxRequestsPerMin); got != float64(DefaultLLMMaxRequestsPerMinute) {
				t.Errorf("%s default = %v, want %d (unchanged)", keyLLMMaxRequestsPerMin, got, DefaultLLMMaxRequestsPerMinute)
			}
			if got := def(keyMaxInterventionsPerMinute); got != float64(DefaultMaxInterventionsPerMinute) {
				t.Errorf("%s default = %v, want %d (unchanged)", keyMaxInterventionsPerMinute, got, DefaultMaxInterventionsPerMinute)
			}
			// The coordinator backstop stays the global flood ceiling: one game at
			// the full frenzy budget fits under its shipped default, and its
			// generous variant holds several (flags.md §3).
			backstop, _ := def("policy.coordinator_backstop_per_minute").(float64)
			if float64(budgetVariantFrenzy) > backstop {
				t.Errorf("frenzy %d is above the coordinator backstop default %v", budgetVariantFrenzy, backstop)
			}
			if generous, _ := variant("policy.coordinator_backstop_per_minute", "generous").(float64); generous < 3*float64(budgetVariantFrenzy) {
				t.Errorf("backstop generous %v holds fewer than 3 frenzy games", generous)
			}
		})
	}
}

// TestBudgetVariants2518_ResolveThroughFlagd selects each new variant the way a
// flag-service overlay does (a defaultVariant substitution, flags.md §13) in a
// copy of the shipped agent.json and resolves it through the flagd file resolver
// and Flags.Evaluate: the snapshot carries 120 and 8.
// UC: UC-070 A4 BR-003
func TestBudgetVariants2518_ResolveThroughFlagd(t *testing.T) {
	path := budgetFlagFiles()[0]
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	flags := doc["flags"].(map[string]any)
	flags[keyLLMMaxRequestsPerMin].(map[string]any)["defaultVariant"] = "interactive"
	flags[keyMaxInterventionsPerMinute].(map[string]any)["defaultVariant"] = "lively"
	body, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	overlay := filepath.Join(t.TempDir(), "agent.json")
	if err := os.WriteFile(overlay, body, 0o600); err != nil {
		t.Fatal(err)
	}

	provider, err := flagd.NewProvider(flagd.WithFileResolver(), flagd.WithOfflineFilePath(overlay))
	if err != nil {
		t.Fatalf("provider: %v", err)
	}
	domain := "budget-variants-" + t.Name()
	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatalf("set provider: %v", err)
	}
	t.Cleanup(func() { provider.Shutdown() })

	snap := New(openfeature.NewClient(domain), nil).Evaluate(context.Background())
	t.Run("UC-070/A4 interactive resolves to 120", func(t *testing.T) {
		if got := snap.LLMGate.MaxRequestsPerMinute; got != budgetVariantInteractive {
			t.Errorf("llm.max_requests_per_minute = %d, want %d", got, budgetVariantInteractive)
		}
	})
	t.Run("UC-070/BR-003 lively resolves to 8", func(t *testing.T) {
		if got := snap.Policy.MaxInterventionsPerMinute; got != budgetVariantLively {
			t.Errorf("policy.max_interventions_per_minute = %d, want %d", got, budgetVariantLively)
		}
	})
}
