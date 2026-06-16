package main

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/joustmania/agent/llm"
)

// inference_prewarm_test.go covers the #1130 startup model pre-warm: it fires
// exactly one background Infer for a real backend, skips entirely for the stub
// (nil delegate), never blocks startup, and swallows errors (best-effort).

// waitForCount spins until the atomic reaches want or the deadline elapses,
// covering the background goroutine's eventual call without a fixed sleep.
func waitForCount(t *testing.T, c *atomic.Int64, want int64) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if c.Load() == want {
			return
		}
		time.Sleep(2 * time.Millisecond)
	}
	t.Fatalf("Infer call count = %d, want %d", c.Load(), want)
}

func TestPrewarmInference_StubBackendNoOp(t *testing.T) {
	// nil delegate (stub default) must not warm at all.
	prewarmInference(context.Background(), nil, "phi4-mini", time.Second)
	// Nothing to assert beyond "did not panic / did not block" — a nil delegate
	// means there is no call to count. The other tests cover the real path.
}

func TestPrewarmInference_RealBackendFiresOnce(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_PREWARM", "") // default-on

	var calls atomic.Int64
	var gotPrompt llm.Prompt
	delegate := func(_ context.Context, p llm.Prompt) (string, error) {
		gotPrompt = p
		calls.Add(1)
		return "pong", nil
	}

	prewarmInference(context.Background(), delegate, "gemma3:4b", time.Second)
	waitForCount(t, &calls, 1)

	if calls.Load() != 1 {
		t.Errorf("Infer called %d times, want exactly 1", calls.Load())
	}
	if gotPrompt.User == "" {
		t.Error("warmup prompt User is empty; want a minimal prompt")
	}
	if gotPrompt.Model != "gemma3:4b" {
		t.Errorf("warmup prompt Model = %q, want gemma3:4b", gotPrompt.Model)
	}
}

func TestPrewarmInference_SwallowsErrors(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_PREWARM", "")

	var calls atomic.Int64
	delegate := func(context.Context, llm.Prompt) (string, error) {
		calls.Add(1)
		return "", errors.New("backend unreachable")
	}

	// A failing backend must not panic or block — the helper returns immediately
	// (goroutine) and the error is logged, not propagated.
	prewarmInference(context.Background(), delegate, "phi4-mini", time.Second)
	waitForCount(t, &calls, 1)
}

func TestPrewarmInference_DoesNotBlock(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_PREWARM", "")

	release := make(chan struct{})
	delegate := func(ctx context.Context, _ llm.Prompt) (string, error) {
		<-release // block until the test releases it
		return "ok", nil
	}

	done := make(chan struct{})
	go func() {
		prewarmInference(context.Background(), delegate, "phi4-mini", 5*time.Second)
		close(done)
	}()

	select {
	case <-done:
		// prewarmInference returned promptly even though Infer is still blocked.
	case <-time.After(time.Second):
		t.Fatal("prewarmInference blocked on the backend call; want background/non-blocking")
	}
	close(release) // let the goroutine finish cleanly
}

func TestPrewarmInference_DisabledByEnv(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_PREWARM", "false")

	var calls atomic.Int64
	delegate := func(context.Context, llm.Prompt) (string, error) {
		calls.Add(1)
		return "", nil
	}

	prewarmInference(context.Background(), delegate, "phi4-mini", time.Second)
	// Give any (incorrectly-spawned) goroutine a chance to run before asserting.
	time.Sleep(50 * time.Millisecond)
	if calls.Load() != 0 {
		t.Errorf("Infer called %d times with prewarm disabled, want 0", calls.Load())
	}
}

func TestPrewarmEnabled(t *testing.T) {
	cases := map[string]bool{
		"":      true,
		"true":  true,
		"1":     true,
		"on":    true,
		"yes":   true,
		"false": false,
		"0":     false,
		"off":   false,
		"no":    false,
		"FALSE": false,
		"junk":  true, // unknown -> fail safe to ON
	}
	for val, want := range cases {
		t.Setenv("AGENT_INFERENCE_PREWARM", val)
		if got := prewarmEnabled(); got != want {
			t.Errorf("prewarmEnabled() with %q = %v, want %v", val, got, want)
		}
	}
}
