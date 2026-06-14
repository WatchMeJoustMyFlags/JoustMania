package inference

import (
	"context"
	"net/http"
	"os"
	"testing"
	"time"

	"github.com/joustmania/agent/llm"
)

// live_test.go runs a REAL Infer against a host Ollama OpenAI-compatible endpoint
// (#1048 live smoke). It is OPT-IN via AGENT_LIVE_OLLAMA=1 and additionally probes
// reachability, so a normal `go test ./...` (no env, no Ollama) SKIPS it cleanly —
// it never fails CI or a dev box without a model. The maintainer runs it via
// `make dry-run` with AGENT_INFERENCE_BACKEND=openai, or directly:
//
//	AGENT_LIVE_OLLAMA=1 go test ./inference -run TestLiveInfer -v
//
// AGENT_INFERENCE_BASE_URL overrides the endpoint (default the host-local Ollama
// /v1); AGENT_INFERENCE_MODEL overrides the model (default phi4-mini).
func TestLiveInfer(t *testing.T) {
	if os.Getenv("AGENT_LIVE_OLLAMA") != "1" {
		t.Skip("set AGENT_LIVE_OLLAMA=1 to run the live Ollama smoke test")
	}

	baseURL := os.Getenv("AGENT_INFERENCE_BASE_URL")
	if baseURL == "" {
		baseURL = "http://localhost:11434/v1"
	}
	model := os.Getenv("AGENT_INFERENCE_MODEL")
	if model == "" {
		model = DefaultModel
	}

	// Reachability probe so a misconfigured/unreachable endpoint skips rather than
	// fails — the live test asserts behavior only when a model is genuinely present.
	probeCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	req, _ := http.NewRequestWithContext(probeCtx, http.MethodGet, baseURL+"/models", nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Skipf("Ollama not reachable at %s: %v", baseURL, err)
	}
	_ = resp.Body.Close()

	b := New(baseURL, os.Getenv("AGENT_INFERENCE_API_KEY"), model)
	ctx, cancel2 := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel2()

	out, err := b.Infer(ctx, llm.Prompt{
		System: "Reply with EXACTLY ONE JSON object and nothing else: {\"flag\":\"sensitivity\",\"value\":2,\"rationale\":\"<one sentence>\"}",
		User:   "The players are tiring; propose one experiment.",
	})
	if err != nil {
		t.Fatalf("live Infer against %s (model=%s) failed: %v", baseURL, model, err)
	}
	if out == "" {
		t.Fatal("live Infer returned empty completion")
	}
	t.Logf("live completion (%d bytes): %s", len(out), out)
}
