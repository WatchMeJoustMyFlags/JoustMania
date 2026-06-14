package decision

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/llm"
)

// probe_backend_test.go covers the #1048 inference-delegate seam on the production
// endpointBackend: with NO delegate (the stub default) Infer returns the honest
// not-implemented sentinel so the chain degrades to rules; with a delegate injected
// (AGENT_INFERENCE_BACKEND=openai) Infer forwards to the real client and surfaces
// its result/error unchanged.

func TestEndpointBackend_Infer_NoDelegateReturnsSentinel(t *testing.T) {
	b := newEndpointBackend(TierPhi, "localhost:11434")
	out, err := b.Infer(context.Background(), llm.Prompt{System: "s", User: "u"})
	if !errors.Is(err, errInferNotImplemented) {
		t.Fatalf("err = %v, want errInferNotImplemented", err)
	}
	if out != "" {
		t.Errorf("out = %q, want empty", out)
	}
}

func TestEndpointBackend_Infer_DelegateForwardsResult(t *testing.T) {
	var gotPrompt llm.Prompt
	b := newEndpointBackendWithInfer(TierPhi, "localhost:11434",
		func(_ context.Context, p llm.Prompt) (string, error) {
			gotPrompt = p
			return "model said this", nil
		})
	out, err := b.Infer(context.Background(), llm.Prompt{System: "sys", User: "usr"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out != "model said this" {
		t.Errorf("out = %q, want delegate result", out)
	}
	if gotPrompt.System != "sys" || gotPrompt.User != "usr" {
		t.Errorf("delegate received %+v, want sys/usr forwarded", gotPrompt)
	}
}

func TestEndpointBackend_Infer_DelegateErrorPropagates(t *testing.T) {
	sentinel := errors.New("endpoint down")
	b := newEndpointBackendWithInfer(TierPhi, "localhost:11434",
		func(context.Context, llm.Prompt) (string, error) { return "", sentinel })
	if _, err := b.Infer(context.Background(), llm.Prompt{}); !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want delegate error propagated", err)
	}
}

// TestDefaultChainWithInfer_InjectsDelegateIntoEveryTier asserts the delegate
// reaches all three tiers (so whichever tier resolves uses the real backend), and
// that nil keeps the sentinel (the stub default — no behavior change).
func TestDefaultChainWithInfer_InjectsDelegateIntoEveryTier(t *testing.T) {
	calls := 0
	delegate := func(context.Context, llm.Prompt) (string, error) {
		calls++
		return "ok", nil
	}
	chain := DefaultChainWithInfer(Endpoints{Cloud: "c:1", Jetson: "j:1", Localhost: "l:1"}, delegate)
	if len(chain) != 3 {
		t.Fatalf("chain length = %d, want 3", len(chain))
	}
	for _, b := range chain {
		if _, err := b.Infer(context.Background(), llm.Prompt{}); err != nil {
			t.Errorf("tier %s Infer error: %v", b.Name(), err)
		}
	}
	if calls != 3 {
		t.Errorf("delegate calls = %d, want 3 (one per tier)", calls)
	}

	// nil delegate -> every tier returns the sentinel (DefaultChain behavior).
	nilChain := DefaultChainWithInfer(Endpoints{Cloud: "c:1", Jetson: "j:1", Localhost: "l:1"}, nil)
	for _, b := range nilChain {
		if _, err := b.Infer(context.Background(), llm.Prompt{}); !errors.Is(err, errInferNotImplemented) {
			t.Errorf("nil-delegate tier %s err = %v, want sentinel", b.Name(), err)
		}
	}
}
