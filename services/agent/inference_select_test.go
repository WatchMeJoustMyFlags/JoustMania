package main

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/inference"
)

// inference_select_test.go covers the backend selection for the offline seams
// (proposer + retro): the stub default, fail-safe on unknown values and on a missing
// key, and the claude path. The choice comes from a fixed inference.backend flag value
// (the AGENT_INFERENCE_BACKEND env is removed, #1907).

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_DefaultIsStub(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	backend, name := selectInferenceBackend(quietLogger(), nil, "") // unset/empty == stub
	if backend != nil || name != "stub" {
		t.Errorf("default = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_UnknownValueFailsSafeToStub(t *testing.T) {
	for _, v := range []string{"gpt-9000", "openai"} { // openai was retired in #1234
		t.Setenv("ANTHROPIC_API_KEY", "sk-test")
		backend, name := selectInferenceBackend(quietLogger(), nil, v)
		if backend != nil || name != "stub" {
			t.Errorf("%s = (%v, %q), want (nil, stub)", v, backend != nil, name)
		}
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_ClaudeWithoutKeyIsStub(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "")
	backend, name := selectInferenceBackend(quietLogger(), nil, "claude")
	if backend != nil || name != "stub" {
		t.Errorf("claude without key = (%v, %q), want (nil, stub)", backend != nil, name)
	}
}

// UC: UC-042 Pre-2
func TestSelectInferenceBackend_ClaudeSelectsRealClient(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	backend, name := selectInferenceBackend(quietLogger(), nil, "Claude") // case-insensitive
	if backend == nil || name != "claude" {
		t.Fatalf("claude = (%v, %q), want a real client", backend != nil, name)
	}
	if !backend.Available() || backend.Model() != inference.DefaultModel {
		t.Errorf("client available=%v model=%q", backend.Available(), backend.Model())
	}
	var _ experiment.Backend = backend
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
			t.Setenv("ANTHROPIC_API_KEY", "sk-test")
			t.Setenv("AGENT_INFERENCE_BASE_URL", tc.url)
			backend, name := selectInferenceBackend(quietLogger(), nil, "claude")
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

// fixedBackendFlag is an inference.backend flag source that always answers one value.
type fixedBackendFlag string

func (f fixedBackendFlag) InferenceBackend(context.Context, string) string { return string(f) }

// selectInferenceBackend selects through a flag source fixed to backend.
func selectInferenceBackend(logger *slog.Logger, tuning *agentTuning, backend string) (*inference.Claude, string) {
	return newTextBackends(logger, tuning, fixedBackendFlag(backend)).Select(context.Background())
}

// sourceFor is a proposer backend source over a test delegate: nil = the stub
// (real=false), else a real backend.
func sourceFor(delegate inferFunc) experiment.BackendSource {
	return func(context.Context) (experiment.Backend, bool) {
		if delegate == nil {
			return proposeBackend{}, false
		}
		return inferBackend{infer: delegate}, true
	}
}
