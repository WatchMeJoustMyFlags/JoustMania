package llmio

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// UC: UC-040 A47 BR-038
func TestSystemPrompt_DefinesWaiting(t *testing.T) {
	t.Run("UC-040/A47 both profiles define the waiting status and the session count", func(t *testing.T) {
		for _, profile := range []string{decide.PromptProfileV1, decide.PromptProfileNeutral} {
			sys := SystemPrompt(profile)
			if !strings.Contains(sys, `the status "waiting" is a Fight Club or Tournament player between its fights: still in the game`) {
				t.Errorf("%s system prompt does not define the waiting status", profile)
			}
			if !strings.Contains(sys, `"waiting=<n>" counts these players`) {
				t.Errorf("%s system prompt does not explain the session line's waiting=<n>", profile)
			}
		}
	})
}
