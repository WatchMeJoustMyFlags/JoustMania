//go:build live

package claude

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// TestLiveDecide calls the real Anthropic API. It is opt-in twice: the `live`
// build tag and an ANTHROPIC_API_KEY in the environment. It costs a few cents.
//
//	ANTHROPIC_API_KEY=... go test -tags live ./decide/backends/claude -run TestLiveDecide -v
//
// CLAUDE_LIVE_MODEL overrides the model (default claude-haiku-4-5).
func TestLiveDecide(t *testing.T) {
	key := os.Getenv("ANTHROPIC_API_KEY")
	if key == "" {
		t.Skip("ANTHROPIC_API_KEY not set")
	}
	model := os.Getenv("CLAUDE_LIVE_MODEL")
	if model == "" {
		model = "claude-haiku-4-5"
	}
	b := New(Config{APIKey: key, Model: model, Timeout: 10 * time.Second, CaptureContent: true})

	req := testRequest()
	// Two calls: the first pays connection setup and the one-time schema
	// compilation, the second is the steady-state in-game latency.
	for i := 0; i < 2; i++ {
		start := time.Now()
		resp, err := b.Decide(context.Background(), req)
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("call %d: Decide: %v (after %v)", i, err, elapsed)
		}
		if err := decide.Validate(req, resp); err != nil {
			t.Fatalf("call %d: Validate: %v", i, err)
		}
		t.Logf("call %d: %s latency=%v answers=%+v rationale=%q", i, model, elapsed, resp.Answers, resp.Rationale)
		if i == 1 && elapsed > DefaultTimeout {
			t.Errorf("steady-state latency %v exceeds the %v in-game budget", elapsed, DefaultTimeout)
		}
	}
}
