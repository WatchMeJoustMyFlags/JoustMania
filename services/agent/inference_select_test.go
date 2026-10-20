package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/llm"
)

// inference_select_test.go covers the #1234 env-driven backend selection for the
// offline seams (proposer + retro): the stub default, fail-safe on unknown values
// and on a missing key, and the claude path.

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_DefaultIsStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "") // unset/empty == stub
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	backend, name := selectInferenceBackend(quietLogger(), nil)
	if backend != nil || name != "stub" {
		t.Errorf("default = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_UnknownValueFailsSafeToStub(t *testing.T) {
	for _, v := range []string{"gpt-9000", "openai"} { // openai was retired in #1234
		t.Setenv("AGENT_INFERENCE_BACKEND", v)
		t.Setenv("ANTHROPIC_API_KEY", "sk-test")
		backend, name := selectInferenceBackend(quietLogger(), nil)
		if backend != nil || name != "stub" {
			t.Errorf("%s = (%v, %q), want (nil, stub)", v, backend != nil, name)
		}
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_ClaudeWithoutKeyIsStub(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "claude")
	t.Setenv("ANTHROPIC_API_KEY", "")
	backend, name := selectInferenceBackend(quietLogger(), nil)
	if backend != nil || name != "stub" {
		t.Errorf("claude without key = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_ClaudeSelectsRealClient(t *testing.T) {
	t.Setenv("AGENT_INFERENCE_BACKEND", "Claude") // case-insensitive
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_MODEL", "claude-sonnet-5-5")
	backend, name := selectInferenceBackend(quietLogger(), nil)
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

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_BaseURL(t *testing.T) {
	// wantBase "" = rejected (stub, the key goes nowhere); otherwise the API root the
	// selected backend sends ANTHROPIC_API_KEY to.
	cases := []struct {
		name, url, wantBase string
	}{
		{"unset keeps the Anthropic API", "", inference.DefaultBaseURL},
		{"http loopback ip (the fake)", "http://127.0.0.1:9999", "http://127.0.0.1:9999"},
		{"http loopback 127/8", "http://127.1.2.3:9999", "http://127.1.2.3:9999"},
		{"http localhost", "http://localhost:9999", "http://localhost:9999"},
		{"uppercase scheme and localhost", "HTTP://LOCALHOST:9999", "HTTP://LOCALHOST:9999"},
		{"http loopback ipv6", "http://[::1]:9999", "http://[::1]:9999"},
		// Go's IP.IsLoopback treats an IPv4-mapped 127/8 address as loopback.
		{"http ipv4-mapped loopback", "http://[::ffff:127.0.0.1]:9999", "http://[::ffff:127.0.0.1]:9999"},
		{"https remote", "https://proxy.example.com/anthropic", "https://proxy.example.com/anthropic"},
		{"https trailing slash trimmed", "https://proxy.example.com/", "https://proxy.example.com"},
		{"http remote host", "http://proxy.example.com", ""},
		{"http remote ip", "http://10.0.0.5:9999", ""},
		{"http unspecified 0.0.0.0", "http://0.0.0.0:9999", ""},
		{"http localhost suffix domain", "http://localhost.evil.com:9999", ""},
		{"http wildcard dns to loopback", "http://127.0.0.1.nip.io:9999", ""},
		{"http trailing-dot localhost", "http://localhost.:9999", ""},
		{"not a url", "::nope", ""},
		{"other scheme", "ftp://127.0.0.1:9999", ""},
		{"no host", "http://", ""},
		{"credentials in url", "http://user:pw@127.0.0.1:9999", ""},
		{"https with credentials", "https://user:pw@proxy.example.com", ""},
		{"query string", "http://127.0.0.1:9999?x=1", ""},
		{"fragment", "http://127.0.0.1:9999#frag", ""},
		{"https fragment", "https://proxy.example.com/#frag", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("AGENT_INFERENCE_BACKEND", "claude")
			t.Setenv("ANTHROPIC_API_KEY", "sk-test")
			t.Setenv("AGENT_INFERENCE_BASE_URL", tc.url)
			backend, name := selectInferenceBackend(quietLogger(), nil)
			if tc.wantBase == "" {
				if backend != nil || name != "stub" {
					t.Fatalf("%q = (backend %v, %q), want rejected (nil, stub)", tc.url, backend != nil, name)
				}
				return
			}
			if backend == nil || name != inference.Name {
				t.Fatalf("%q = (nil, %q), want claude backend at %q", tc.url, name, tc.wantBase)
			}
			if got := backend.BaseURL(); got != tc.wantBase {
				t.Fatalf("%q: backend base URL = %q, want %q", tc.url, got, tc.wantBase)
			}
		})
	}
}
