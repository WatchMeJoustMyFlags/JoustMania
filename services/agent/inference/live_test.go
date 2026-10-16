package inference

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/joustmania/agent/llm"
)

// TestLiveClaude calls the real Anthropic API. Opt-in (it costs money):
//
//	AGENT_LIVE_CLAUDE=1 ANTHROPIC_API_KEY=... go test ./inference -run TestLiveClaude -v
//
// AGENT_INFERENCE_MODEL overrides the model.
func TestLiveClaude(t *testing.T) {
	if os.Getenv("AGENT_LIVE_CLAUDE") != "1" || os.Getenv("ANTHROPIC_API_KEY") == "" {
		t.Skip("set AGENT_LIVE_CLAUDE=1 and ANTHROPIC_API_KEY to run the live Claude smoke test")
	}
	c := NewClaude(Config{
		APIKey: os.Getenv("ANTHROPIC_API_KEY"),
		Model:  os.Getenv("AGENT_INFERENCE_MODEL"),
		Effort: DefaultEffort,
	})
	out, err := c.Infer(context.Background(), llm.Prompt{
		System: "Reply with exactly one JSON object and no prose.",
		User:   `Return {"ok": true}.`,
	})
	if err != nil {
		t.Fatalf("Infer: %v", err)
	}
	if !strings.Contains(out, "ok") {
		t.Fatalf("unexpected reply: %q", out)
	}
	t.Logf("model %s replied: %s", c.Model(), out)
}
