//go:build live

package openai

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// TestLiveDecide calls a real OpenAI-compatible endpoint. It is opt-in twice:
// the `live` build tag and DECISION_OPENAI_API_KEY in the environment. It costs
// a few cents.
//
//	DECISION_OPENAI_API_KEY=... go test -tags live ./decide/backends/openai -run TestLiveDecide -v
//
// DECISION_OPENAI_BASE_URL (default DefaultBaseURL) and DECISION_OPENAI_MODEL
// (default gpt-4.1-mini) pick the vendor and model, e.g. for Gemini:
//
//	DECISION_OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
//	DECISION_OPENAI_MODEL=gemini-2.5-flash
func TestLiveDecide(t *testing.T) {
	key := os.Getenv("DECISION_OPENAI_API_KEY")
	if key == "" {
		t.Skip("DECISION_OPENAI_API_KEY not set")
	}
	base := os.Getenv("DECISION_OPENAI_BASE_URL")
	if base == "" {
		base = DefaultBaseURL
	}
	model := os.Getenv("DECISION_OPENAI_MODEL")
	if model == "" {
		model = "gpt-4.1-mini"
	}
	b := New(Config{APIKey: key, BaseURL: base, Model: model, Timeout: 10 * time.Second, CaptureContent: true})
	t.Logf("provider=%s base=%s model=%s", b.Provider(), base, model)

	req := testRequest()
	// Two calls: the first pays connection setup (and, for a vendor that
	// rejects json_schema, the downgrade), the second is steady state.
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
		t.Logf("call %d: latency=%v json_object_fallback=%v answers=%+v rationale=%q",
			i, elapsed, b.schemaRejected.Load(), resp.Answers, resp.Rationale)
		if i == 1 && elapsed > DefaultTimeout {
			t.Errorf("steady-state latency %v exceeds the %v in-game budget", elapsed, DefaultTimeout)
		}
	}
}
