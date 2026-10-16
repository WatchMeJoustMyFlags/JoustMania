package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/llm"
)

// inference_select_test.go covers the #1234 env-driven backend selection for the
// offline seams (proposer + retro): the stub default, fail-safe on unknown values
// and on a missing key, and the claude path.

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func TestSelectInferenceBackend_DefaultIsStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "") // unset/empty == stub
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	backend, name := selectInferenceBackend(quietLogger())
	if backend != nil || name != "stub" {
		t.Errorf("default = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

func TestSelectInferenceBackend_UnknownValueFailsSafeToStub(t *testing.T) {
	for _, v := range []string{"gpt-9000", "openai"} { // openai was retired in #1234
		t.Setenv("AGENT_INFERENCE_BACKEND", v)
		t.Setenv("ANTHROPIC_API_KEY", "sk-test")
		backend, name := selectInferenceBackend(quietLogger())
		if backend != nil || name != "stub" {
			t.Errorf("%s = (%v, %q), want (nil, stub)", v, backend != nil, name)
		}
	}
}

func TestSelectInferenceBackend_ClaudeWithoutKeyIsStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "claude")
	t.Setenv("ANTHROPIC_API_KEY", "")
	backend, name := selectInferenceBackend(quietLogger())
	if backend != nil || name != "stub" {
		t.Errorf("claude without key = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

func TestSelectInferenceBackend_ClaudeSelectsRealClient(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "Claude") // case-insensitive
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_MODEL", "claude-sonnet-5-5")
	backend, name := selectInferenceBackend(quietLogger())
	if backend == nil || name != "claude" {
		t.Fatalf("claude = (%v, %q), want a real client", backend != nil, name)
	}
	if !backend.Available() || backend.Model() != "claude-sonnet-5-5" {
		t.Errorf("client available=%v model=%q", backend.Available(), backend.Model())
	}
	var _ experiment.Backend = backend
}

// proposeBackendFor: nil delegate -> the inert stub (returns the not-implemented
// sentinel); a delegate -> a Backend that forwards to it.
func TestProposeBackendFor(t *testing.T) {
	// nil -> stub (degrades to no proposal).
	stub := proposeBackendFor(nil)
	if _, err := stub.Infer(context.Background(), llm.Prompt{}); err == nil {
		t.Error("stub backend Infer returned nil error; want sentinel")
	}

	// delegate -> forwards.
	want := "raw model text"
	real := proposeBackendFor(func(context.Context, llm.Prompt) (string, error) {
		return want, nil
	})
	out, err := real.Infer(context.Background(), llm.Prompt{})
	if err != nil {
		t.Fatalf("delegate-backed Infer error: %v", err)
	}
	if out != want {
		t.Errorf("out = %q, want %q", out, want)
	}

	// Both satisfy experiment.Backend (compile-time + runtime).
	var _ experiment.Backend = stub
	var _ experiment.Backend = real
}

func TestProposeBackendFor_DelegateErrorPropagates(t *testing.T) {
	sentinel := errors.New("boom")
	b := proposeBackendFor(func(context.Context, llm.Prompt) (string, error) { return "", sentinel })
	if _, err := b.Infer(context.Background(), llm.Prompt{}); !errors.Is(err, sentinel) {
		t.Fatalf("err = %v, want delegate error", err)
	}
}
