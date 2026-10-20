package experiment

import (
	"context"
	"strings"
	"testing"

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
		ProposerConfig{Permit: allowPropose, Engine: "stub"})

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
