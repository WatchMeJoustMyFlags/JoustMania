package main

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/llm"
)

// inference_select_test.go covers the #1048 env-driven backend selection in main:
// the default/stub path returns NO delegate (so both seams keep their inert stubs,
// no behavior change) and the openai path returns a real delegate that feeds both.

func TestSelectInferenceBackend_DefaultIsStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "") // unset/empty == stub
	delegate, name := selectInferenceBackend()
	if delegate != nil {
		t.Error("delegate != nil for default backend; want nil (stub)")
	}
	if name != "stub" {
		t.Errorf("name = %q, want stub", name)
	}
}

func TestSelectInferenceBackend_UnknownValueFailsSafeToStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "gpt-9000") // typo / unknown
	delegate, name := selectInferenceBackend()
	if delegate != nil || name != "stub" {
		t.Errorf("unknown backend = (%v, %q), want (nil, stub)", delegate != nil, name)
	}
}

func TestSelectInferenceBackend_OpenAISelectsRealDelegate(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "OpenAI") // case-insensitive
	delegate, name := selectInferenceBackend()
	if delegate == nil {
		t.Fatal("delegate == nil for openai backend; want a real client")
	}
	if name != "openai" {
		t.Errorf("name = %q, want openai", name)
	}
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
