package experiment

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/llm"
)

// capturingBackend records the prompt the proposer sends to the model.
type capturingBackend struct {
	prompts []llm.Prompt
}

func (b *capturingBackend) Infer(_ context.Context, p llm.Prompt) (string, error) {
	b.prompts = append(b.prompts, p)
	return "no proposal", nil
}

// The rendered PRIOR GAMES block (real window plus, for a shadow game, its own
// experiment's linked set) reaches the model in the proposer prompt, next to the
// flag vocabulary.
// UC: UC-042 main
func TestProposeOnce_PriorGamesBlockReachesPrompt(t *testing.T) {
	backend := &capturingBackend{}
	p, _, _, _ := proposerWithRecorder(t, backend)
	block := "PRIOR GAMES (real games only, most recent last; game-level aggregates, no player identity):\n" +
		"  1. joust, 4 players, outcome=winner\n" +
		"LINKED SET exp_abc123 (shadow games of this experiment):\n" +
		"  1. joust, 4 players, outcome=tie"

	p.ProposeOnce(context.Background(), Narrative{GameMode: "joust", ContextBlock: block},
		ProposerConfig{Permit: allowPropose})

	if len(backend.prompts) != 1 {
		t.Fatalf("model calls = %d, want 1", len(backend.prompts))
	}
	user := backend.prompts[0].User
	for _, want := range []string{"PRIOR GAMES (real games only", "LINKED SET exp_abc123", "outcome=tie"} {
		if !strings.Contains(user, want) {
			t.Errorf("prompt user part lacks %q:\n%s", want, user)
		}
	}
	if !strings.Contains(backend.prompts[0].System, "death_grace_period_seconds") {
		t.Errorf("prompt system part lacks the flag vocabulary")
	}
}

// The proposer prompt shows each catalog flag with its registry description,
// its declared variants and its range, read from the flag service (UC-042
// BR-008, #2345): end to end from registry.yaml through the fake's GET /game.
// UC: UC-042 BR-008
func TestProposeOnce_PromptCarriesCatalogDescriptions(t *testing.T) {
	backend := &capturingBackend{}
	p, _, _, _ := proposerWithRecorder(t, backend)
	p.ProposeOnce(context.Background(), Narrative{GameMode: "joust"}, ProposerConfig{Permit: allowPropose})
	if len(backend.prompts) != 1 {
		t.Fatalf("model calls = %d, want 1", len(backend.prompts))
	}
	sys := backend.prompts[0].System
	for _, want := range []string{
		"  - death_grace_period_seconds: seconds a player cannot die after swapping team, turning zombie or respawning (variants ",
		"; range 0 to 5)",
		"  - invincibility_seconds: seconds Tournament and Fight Club fighters cannot lose",
	} {
		if !strings.Contains(sys, want) {
			t.Errorf("UC-042/BR-008 proposer prompt lacks %q:\n%s", want, sys)
		}
	}
}

// One catalog line renders key, description, variants (name=value, an object
// by its kind) and range, and stays bounded whatever the service sends: a long
// description is cut and folded to one line, and at most ten variants show
// (UC-042 BR-008, #2345).
// UC: UC-042 BR-008
func TestBuildProposalPrompt_CatalogLineBounded(t *testing.T) {
	t.Run("UC-042/BR-008 key, description, variants and range", func(t *testing.T) {
		vocab := map[string]flagservice.FlagView{
			"shield.energy_budget_seconds": {
				Policy:        &flagservice.Policy{Description: "hold mode: total shield seconds", Bounds: flagservice.Bounds{Range: []float64{3, 30}}},
				Variants:      []string{"tight", "default"},
				VariantValues: map[string]json.RawMessage{"tight": json.RawMessage("10"), "default": json.RawMessage("15")},
			},
			"zombie.initial_count": {
				Policy:        &flagservice.Policy{Description: "starting zombies", Bounds: flagservice.Bounds{Range: []float64{1, 5}, Integer: true}},
				Variants:      []string{"one"},
				VariantValues: map[string]json.RawMessage{"one": json.RawMessage("1")},
			},
			"windows": {
				Policy:        &flagservice.Policy{Description: "music phases"},
				Variants:      []string{"default"},
				VariantValues: map[string]json.RawMessage{"default": json.RawMessage(`{"a": 1}`)},
			},
			"bare": {},
		}
		sys := buildProposalPrompt(Narrative{}, vocab).System
		for _, want := range []string{
			"  - shield.energy_budget_seconds: hold mode: total shield seconds (variants tight=10, default=15; range 3 to 30)\n",
			"  - zombie.initial_count: starting zombies (variants one=1; range 1 to 5 (whole numbers))\n",
			"  - windows: music phases (variants default=object)\n",
			"  - bare\n",
		} {
			if !strings.Contains(sys, want) {
				t.Errorf("prompt lacks %q:\n%s", want, sys)
			}
		}
	})
	t.Run("UC-042/BR-008 an oversized row is cut to a bounded line", func(t *testing.T) {
		names := make([]string, 0, 50)
		values := map[string]json.RawMessage{}
		for i := 0; i < 50; i++ {
			n := strings.Repeat("v", 5) + string(rune('A'+i%26)) + string(rune('a'+i/26))
			names = append(names, n)
			values[n] = json.RawMessage(`"` + strings.Repeat("x", 200) + `"`)
		}
		f := flagservice.FlagView{
			Policy:        &flagservice.Policy{Description: "line one\nline two " + strings.Repeat("y", 5000)},
			Variants:      names,
			VariantValues: values,
		}
		line := catalogLine("k", f)
		if strings.Contains(line, "\n") || !strings.Contains(line, "line one line two") {
			t.Fatalf("description not folded to one line: %q", line)
		}
		// key + description cap + 10 variants of (name + value caps) + framing.
		if limit := 600; len(line) > limit {
			t.Fatalf("catalog line is %d bytes, want <= %d: %q", len(line), limit, line)
		}
		if !strings.Contains(line, ", ...)") {
			t.Fatalf("hidden variants not marked: %q", line)
		}
	})
}
