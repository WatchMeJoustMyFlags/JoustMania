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

// #2519: the paid backends' own decide caps, decision.claude.max_requests_per_minute
// and decision.openai.max_requests_per_minute. Tighten-only under the global
// llm.max_requests_per_minute (FR-152 (c)); operator-written, never by the agent
// (C-034). The base file ships `stage` (30) so `frenzy` on jev does not let a
// switch to a paid backend spend 600 calls a minute; the ci file ships `inherit`
// so the service-test journeys at `journey` 600 are not capped.

var backendCapKeys = []string{keyDecisionClaudeMaxRequestsPerMin, keyDecisionOpenAIMaxRequestsPerMin}

// UC: UC-070 BR-003
// NFR: NFR-011
func TestBudgetBackend2519_DeclaredInBothFlagFiles(t *testing.T) {
	wantDefault := map[string]string{
		budgetFlagFiles()[0]: "stage",
		budgetFlagFiles()[1]: "inherit",
	}
	wantVariants := map[string]float64{"inherit": MaxRequestsPerMinuteInherit, "conservative": 3, "default": 6, "stage": 30}
	for _, path := range budgetFlagFiles() {
		flags := readAgentFlagFile(t, path)
		for _, key := range backendCapKeys {
			t.Run("UC-070/BR-003 "+path+" "+key, func(t *testing.T) {
				flag, ok := flags[key]
				if !ok {
					t.Fatalf("%s is not declared", key)
				}
				variants, _ := flag["variants"].(map[string]any)
				if len(variants) != len(wantVariants) {
					t.Errorf("variants = %v, want %v", variants, wantVariants)
				}
				for name, want := range wantVariants {
					if got := variants[name]; got != want {
						t.Errorf("variant %s = %v, want %v", name, got, want)
					}
				}
				if got := flag["defaultVariant"]; got != wantDefault[path] {
					t.Errorf("defaultVariant = %v, want %s", got, wantDefault[path])
				}
			})
		}
	}
	// The Go default mirrors the base file's defaultVariant (FR-151).
	if DefaultDecisionClaudeMaxRequestsPerMinute != 30 || DefaultDecisionOpenAIMaxRequestsPerMinute != 30 {
		t.Errorf("Go defaults = %d/%d, want 30/30 (the base file's stage)",
			DefaultDecisionClaudeMaxRequestsPerMinute, DefaultDecisionOpenAIMaxRequestsPerMinute)
	}
}

// TestBudgetBackend2519_ResolveThroughFlagd selects the variants the way a
// flag-service overlay does (a defaultVariant substitution, flags.md §13) in a copy
// of the shipped agent.json and resolves them through the flagd file resolver and
// Flags.Evaluate: claude `inherit` has no cap of its own, openai `conservative` is 3.
// UC: UC-070 BR-003
func TestBudgetBackend2519_ResolveThroughFlagd(t *testing.T) {
	raw, err := os.ReadFile(budgetFlagFiles()[0])
	if err != nil {
		t.Fatal(err)
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	flags := doc["flags"].(map[string]any)
	flags[keyDecisionClaudeMaxRequestsPerMin].(map[string]any)["defaultVariant"] = "inherit"
	flags[keyDecisionOpenAIMaxRequestsPerMin].(map[string]any)["defaultVariant"] = "conservative"
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
	domain := "budget-backend-" + t.Name()
	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatalf("set provider: %v", err)
	}
	t.Cleanup(func() { provider.Shutdown() })

	d := New(openfeature.NewClient(domain), nil).Evaluate(context.Background()).Decision
	t.Run("UC-070/BR-003 claude inherit has no cap of its own", func(t *testing.T) {
		if d.Claude.HasMaxRequestsPerMinute {
			t.Errorf("claude cap = %d, want none (inherit)", d.Claude.MaxRequestsPerMinute)
		}
	})
	t.Run("UC-070/BR-003 openai conservative resolves to 3", func(t *testing.T) {
		if !d.OpenAI.HasMaxRequestsPerMinute || d.OpenAI.MaxRequestsPerMinute != 3 {
			t.Errorf("openai cap = %d,%v, want 3,true", d.OpenAI.MaxRequestsPerMinute, d.OpenAI.HasMaxRequestsPerMinute)
		}
	})
}
