//go:build live

package claude

import (
	"context"
	"os"
	"strconv"
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
// CLAUDE_LIVE_TIMEOUT_MS sets the in-game budget the steady-state call must fit
// (default DefaultTimeout, which mirrors the decision.claude.timeout_ms flag
// default). Set it to the value the stage runs with to check that budget.
func TestLiveDecide(t *testing.T) {
	key := os.Getenv("ANTHROPIC_API_KEY")
	if key == "" {
		t.Skip("ANTHROPIC_API_KEY not set")
	}
	model := os.Getenv("CLAUDE_LIVE_MODEL")
	if model == "" {
		model = "claude-haiku-4-5"
	}
	budget := DefaultTimeout
	if v := os.Getenv("CLAUDE_LIVE_TIMEOUT_MS"); v != "" {
		ms, err := strconv.Atoi(v)
		if err != nil || ms <= 0 {
			t.Fatalf("CLAUDE_LIVE_TIMEOUT_MS=%q: want a positive integer", v)
		}
		budget = time.Duration(ms) * time.Millisecond
	}
	// The call itself gets generous headroom so a slow call is measured and
	// reported against the budget instead of being cut off.
	b := New(Config{APIKey: key, Model: model, Timeout: 2*budget + 5*time.Second, CaptureContent: true})

	req := testRequest()
	// Two calls: the first pays connection setup (and, on a new schema, the
	// one-time schema compilation), the second is the steady-state in-game
	// latency.
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
		a := attrs(lastChatSpan(t))
		t.Logf("call %d: %s latency=%v budget=%v input_tokens=%d (cache read %d, write %d) output_tokens=%d answers=%+v rationale=%q",
			i, model, elapsed.Round(time.Millisecond), budget,
			a["gen_ai.usage.input_tokens"].AsInt64(),
			a["gen_ai.usage.cache_read.input_tokens"].AsInt64(),
			a["gen_ai.usage.cache_creation.input_tokens"].AsInt64(),
			a["gen_ai.usage.output_tokens"].AsInt64(),
			resp.Answers, resp.Rationale)
		if i == 1 && elapsed > budget {
			t.Errorf("steady-state latency %v exceeds the %v in-game budget", elapsed, budget)
		}
	}
}
