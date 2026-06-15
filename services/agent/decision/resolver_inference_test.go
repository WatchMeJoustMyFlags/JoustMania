package decision

import (
	"context"
	"net"
	"testing"

	"github.com/joustmania/agent/llm"
)

// resolver_inference_test.go covers the #964 inference-backend resolver tier: when
// AGENT_INFERENCE_BACKEND=openai, the resolver's chain TOP is the configured backend
// (named after AGENT_INFERENCE_MODEL, probed at the AGENT_INFERENCE_BASE_URL host:port,
// Infer-delegating to the real client). resolve() must START at that tier regardless of
// the agent.model flag (so a default phi4-mini flag no longer skips it), report it as
// inference.used when reachable, and degrade to rules when it (and the legacy chain) are
// unreachable — graceful degradation unchanged. The stub default (no inference tier) is
// covered for byte-identical behavior.

// listenLocal opens a real loopback TCP listener and returns its host:port plus a
// cleanup. The resolver's production endpointBackend probes reachability with a real
// TCP dial, so an open listener is the dependency-free way to make a tier "reachable"
// in a unit test (no mock dialer plumbing for endpointBackend).
func listenLocal(t *testing.T) (addr string, closeFn func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	return ln.Addr().String(), func() { _ = ln.Close() }
}

// TestInferenceProbeAddr parses an OpenAI-compatible base URL down to the host:port a
// TCP probe should dial (#964 gap 1), supplying the scheme default port when omitted
// and returning "" (fail-safe) for an unusable URL.
func TestInferenceProbeAddr(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"ollama lan with port and /v1", "http://192.168.224.1:11434/v1", "192.168.224.1:11434"},
		{"host docker internal", "http://host.docker.internal:11434/v1", "host.docker.internal:11434"},
		{"https default port", "https://api.example.com/v1", "api.example.com:443"},
		{"http default port", "http://example.com/v1", "example.com:80"},
		{"trailing space tolerated", "  http://h:9/v1  ", "h:9"},
		{"empty -> empty", "", ""},
		{"no scheme/host -> empty", "/v1", ""},
		{"garbage -> empty", "::::", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := InferenceProbeAddr(tc.in); got != tc.want {
				t.Errorf("InferenceProbeAddr(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// TestInferenceResolver_ReachableStartsAtInferenceTier: with the inference backend
// configured and reachable, resolve() returns the inference tier as inference.used and
// a non-nil Backend that calls the real delegate — even though the agent.model flag is
// the default phi4-mini (which previously selected the unreachable localhost tier and
// degraded to rules). This is the #964 gaps 1+3 fix: the configured backend IS the
// inference path, and AGENT_INFERENCE_MODEL is the single source of truth for the tier.
func TestInferenceResolver_ReachableStartsAtInferenceTier(t *testing.T) {
	addr, closeFn := listenLocal(t)
	defer closeFn()

	const model = "gemma4:latest"
	called := false
	delegate := func(context.Context, llm.Prompt) (string, error) {
		called = true
		return `{"intervention":"noop"}`, nil
	}
	// Legacy endpoints all unreachable (the dev default) — only the configured
	// inference tier is up. resolve() must still pick it.
	r := NewInferenceResolver(InferenceTier{Model: model, Addr: addr, Infer: delegate},
		Endpoints{}, 0)
	r.Refresh(context.Background())

	// The default model flag is phi4-mini; the inference tier must win regardless.
	res := r.resolve("phi4-mini")
	if res.Used != model {
		t.Fatalf("inference.used = %q, want %q (configured inference tier)", res.Used, model)
	}
	if res.FallbackReason != "" {
		t.Errorf("fallback_reason = %q, want empty (configured tier reachable)", res.FallbackReason)
	}
	if res.Backend == nil {
		t.Fatal("resolved Backend is nil; decide() would degrade to rules (mode=rules) — the #964 bug")
	}
	out, err := res.Backend.Infer(context.Background(), llm.Prompt{})
	if err != nil || !called {
		t.Errorf("resolved Backend.Infer did not reach the real delegate: out=%q err=%v called=%v", out, err, called)
	}
}

// TestInferenceResolver_UnreachableDegradesToRules: when the configured inference tier
// AND the legacy chain are unreachable, resolve() bottoms out at rules with
// no_backend_available — the unchanged graceful-degradation path (mode=rules).
func TestInferenceResolver_UnreachableDegradesToRules(t *testing.T) {
	delegate := func(context.Context, llm.Prompt) (string, error) { return "", nil }
	// An addr that nothing is listening on (port 1 on loopback) -> probe fails.
	r := NewInferenceResolver(InferenceTier{Model: "gemma4:latest", Addr: "127.0.0.1:1", Infer: delegate},
		Endpoints{}, 0)
	r.Refresh(context.Background())

	res := r.resolve("phi4-mini")
	if res.Used != InferenceRules || res.FallbackReason != FallbackNoBackend {
		t.Errorf("unreachable resolve = %+v, want rules/%s", res, FallbackNoBackend)
	}
	if res.Backend != nil {
		t.Error("Backend should be nil when the whole chain is unreachable (degrade to rules)")
	}
}

// TestInferenceResolver_DegradesToLegacyTier: with the configured inference tier down
// but a legacy tier (localhost/phi) up, resolve() degrades down the chain rather than
// straight to rules — the fallback chain below the configured backend stays intact.
func TestInferenceResolver_DegradesToLegacyTier(t *testing.T) {
	legacyAddr, closeFn := listenLocal(t)
	defer closeFn()

	delegate := func(context.Context, llm.Prompt) (string, error) { return "ok", nil }
	r := NewInferenceResolver(
		InferenceTier{Model: "gemma4:latest", Addr: "127.0.0.1:1", Infer: delegate}, // configured tier down
		Endpoints{Localhost: legacyAddr},                                            // phi4-mini tier up
		0)
	r.Refresh(context.Background())

	res := r.resolve("phi4-mini")
	if res.Used != TierPhi {
		t.Fatalf("inference.used = %q, want %q (degraded to reachable legacy tier)", res.Used, TierPhi)
	}
	if res.FallbackReason != FallbackEndpointUnreachable {
		t.Errorf("fallback_reason = %q, want %q", res.FallbackReason, FallbackEndpointUnreachable)
	}
	if res.Backend == nil {
		t.Error("resolved legacy Backend is nil")
	}
}

// TestInferenceResolver_ModelNameCollidesWithLegacyTier: when AGENT_INFERENCE_MODEL
// equals a legacy tier name (the default "phi4-mini" == TierPhi when the model is
// unset), the configured tier and the legacy rung would share one availability-map
// key. NewInferenceResolver must drop the same-named legacy rung so the reachable
// configured tier is NOT clobbered by the unreachable legacy localhost probe. Without
// the fix this resolves to rules (no_backend_available) despite a reachable backend.
func TestInferenceResolver_ModelNameCollidesWithLegacyTier(t *testing.T) {
	addr, closeFn := listenLocal(t)
	defer closeFn()

	called := false
	delegate := func(context.Context, llm.Prompt) (string, error) {
		called = true
		return `{"intervention":"noop"}`, nil
	}
	// Configured model name == TierPhi. Legacy localhost points at an unreachable
	// port (the container/dev reality) — last-write-wins would otherwise clobber the
	// configured tier's availability under the shared "phi4-mini" key.
	r := NewInferenceResolver(
		InferenceTier{Model: TierPhi, Addr: addr, Infer: delegate},
		Endpoints{Localhost: "127.0.0.1:1"},
		0)
	r.Refresh(context.Background())

	res := r.resolve(TierPhi)
	if res.Backend == nil {
		t.Fatal("Backend nil: same-named legacy tier clobbered the reachable configured tier (the #964 collision bug)")
	}
	if res.Used != TierPhi || res.FallbackReason != "" {
		t.Errorf("resolve = %+v, want %s with empty fallback (configured tier reachable)", res, TierPhi)
	}
	if _, err := res.Backend.Infer(context.Background(), llm.Prompt{}); err != nil || !called {
		t.Errorf("resolved Backend did not reach the configured delegate: err=%v called=%v", err, called)
	}
}

// TestStubResolver_NoInferenceTier_Unchanged: the stub default (NewResolver, no
// inference tier) selects the tier the agent.model flag names, exactly as before #964 —
// the default-off path is byte-identical (regression guard for gap 1+3 not leaking into
// the stub build).
func TestStubResolver_NoInferenceTier_Unchanged(t *testing.T) {
	r := NewResolver(DefaultChainWithInfer(Endpoints{}, nil), 0)
	if r.inferenceTier != "" {
		t.Fatalf("stub resolver inferenceTier = %q, want empty", r.inferenceTier)
	}
	// resolveTierFromModel must follow the model flag (phi4-mini -> the phi tier index 2),
	// not jump to an inference tier.
	if got := r.resolveTierFromModel("phi4-mini"); got != 2 {
		t.Errorf("stub resolveTierFromModel(phi4-mini) = %d, want 2 (TierPhi)", got)
	}
	if got := r.resolveTierFromModel("claude"); got != 0 {
		t.Errorf("stub resolveTierFromModel(claude) = %d, want 0 (TierCloud)", got)
	}
}
