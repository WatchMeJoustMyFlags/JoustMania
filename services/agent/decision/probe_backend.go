package decision

import (
	"context"
	"errors"
	"net"
	"time"

	"github.com/joustmania/agent/llm"
)

// probe_backend.go is the PRODUCTION inference probe (#741): a Backend whose
// reachability is a bounded TCP dial to a tier's endpoint. It is the only real
// (network-touching) Backend; tests use a fake that implements the same interface
// with no I/O (see resolver_test.go). The dial is intentionally minimal — a
// successful TCP connect is "reachable enough" for #741, which only needs to know
// WHICH tier #739 would call. #739 will replace this with a real model-health
// check (e.g. an Ollama /api/tags or a cloud auth ping) once a backend call exists;
// until then a TCP connect is the honest, dependency-free liveness signal, and in
// dev every endpoint is unreachable so the chain degrades to rules exactly as #741
// requires.

// probeDialTimeout bounds a single tier's availability dial. It is short because
// the probe runs on the background refresh ticker, not the decision hot path, but
// a hung dial would still delay the whole refresh sweep (tiers are probed
// sequentially), so each is capped. 2s tolerates a slow-but-present endpoint
// without letting an unplugged one stall the sweep for long.
const probeDialTimeout = 2 * time.Second

// Endpoints carries the dial addresses for the three llm tiers (#741). main.go
// fills them from env with the documented defaults; an empty address makes that
// tier permanently unreachable (the probe short-circuits to false), which is the
// correct behavior when a tier is not configured (e.g. no Jetson on the network).
type Endpoints struct {
	// Cloud is the cloud inference endpoint (claude/copilot, #742). Credential-
	// blocked today, so there is no sensible dev default — empty unless configured,
	// which keeps the cloud tier permanently unreachable and the chain degrades past
	// it. main.go reads AGENT_CLOUD_ENDPOINT.
	Cloud string
	// Jetson is gemma3:4b's host:port on the Jetson (#738). Default
	// "jetson:11434" (the Ollama port) — unresolvable in dev, so the tier is
	// unreachable and the chain degrades to localhost. main.go reads AGENT_JETSON_ENDPOINT.
	Jetson string
	// Localhost is phi4-mini's host:port on the local box (#739). Default
	// "localhost:11434" (Ollama) — also unreachable in dev unless a model server is
	// actually running locally. main.go reads AGENT_LOCALHOST_ENDPOINT.
	Localhost string
}

// endpointBackend is the production Backend: a named tier whose Available is a
// bounded TCP dial of addr. An empty addr is permanently unreachable.
type endpointBackend struct {
	name string
	addr string
	// dial is the dialer seam. Production uses net.Dialer.DialContext; it is a field
	// so a future test of the real backend (not the resolver) could stub it, mirroring
	// the Backend interface's own test seam. Nil means "use the default dialer".
	dial func(ctx context.Context, addr string) (net.Conn, error)
	// infer is the INJECTED real inference call (#1048). When non-nil, Infer
	// delegates to it (the shared inference.OpenAIBackend client, OpenAI-compatible
	// over HTTP) instead of returning errInferNotImplemented. main.go injects the
	// SAME client instance into every chain tier when AGENT_INFERENCE_BACKEND=openai,
	// so a resolved+reachable tier produces real model text; nil (the default,
	// AGENT_INFERENCE_BACKEND=stub) keeps the honest not-implemented sentinel and the
	// chain degrades to rules exactly as before — no behavior change.
	infer func(ctx context.Context, prompt llm.Prompt) (string, error)
}

// newEndpointBackend builds a production probe for one tier. An empty addr yields a
// backend that is always unreachable (Available short-circuits false) — the correct
// state for an unconfigured tier (no cloud credentials, no Jetson on the LAN).
func newEndpointBackend(name, addr string) *endpointBackend {
	return &endpointBackend{name: name, addr: addr}
}

// newEndpointBackendWithInfer builds a production probe whose Infer delegates to the
// supplied inference function (#1048). It is the real-backend variant of
// newEndpointBackend: Available is still the TCP dial, but a resolved+reachable tier
// now calls a real OpenAI-compatible client instead of returning the sentinel. A nil
// inferFn is tolerated (Infer then falls back to the sentinel error).
func newEndpointBackendWithInfer(name, addr string, inferFn func(ctx context.Context, prompt llm.Prompt) (string, error)) *endpointBackend {
	return &endpointBackend{name: name, addr: addr, infer: inferFn}
}

func (e *endpointBackend) Name() string { return e.name }

// Available dials the tier's endpoint with a bounded timeout and reports whether
// the connect succeeded. An empty addr is unreachable without dialing. The
// connection is closed immediately — this is a liveness probe, not a session.
// Honors ctx (cancellation / deadline) via DialContext, layered under the per-dial
// timeout so the sweep is bounded even with a background ctx that never cancels.
func (e *endpointBackend) Available(ctx context.Context) bool {
	if e.addr == "" {
		return false
	}
	dialCtx, cancel := context.WithTimeout(ctx, probeDialTimeout)
	defer cancel()

	dial := e.dial
	if dial == nil {
		var d net.Dialer
		dial = func(ctx context.Context, addr string) (net.Conn, error) {
			return d.DialContext(ctx, "tcp", addr)
		}
	}
	conn, err := dial(dialCtx, e.addr)
	if err != nil {
		return false
	}
	_ = conn.Close()
	return true
}

// errInferNotImplemented is returned by endpointBackend.Infer until a real
// backend client exists. llm_decide treats ANY Infer error as "this tier could
// not produce a usable answer" and falls back to the rules engine with a
// recorded reason — so a production agent that resolves a reachable endpoint
// (its TCP port is open) but has no inference client yet degrades SAFELY to
// rules rather than dispatching anything. It is a sentinel so callers/tests can
// errors.Is it.
var errInferNotImplemented = errors.New("decision: endpoint inference not implemented (blocked on #738 Jetson / #742 cloud)")

// Infer is the PRODUCTION inference call seam (#739/#1048). When an inference
// delegate was injected (newEndpointBackendWithInfer, AGENT_INFERENCE_BACKEND=openai),
// it forwards prompt to the real OpenAI-compatible client (#1048,
// inference.OpenAIBackend) and returns the model's raw response for decode.go to
// parse defensively. When NO delegate is injected (the default stub backend) it
// returns errInferNotImplemented — the honest, safe state #738 (Jetson) / #742
// (cloud) historically left it in: a tier whose endpoint TCP-dials open (so
// Available reports it reachable, and inference.used names it) but has no client
// cannot fabricate a Decision, so llm_decide falls back to rules. Either way a
// delegate error (unreachable / non-2xx / timeout / unparseable) is just another
// Infer error and degrades to rules SAFELY — the call site already treats every
// error identically.
func (e *endpointBackend) Infer(ctx context.Context, prompt llm.Prompt) (string, error) {
	if e.infer != nil {
		return e.infer(ctx, prompt)
	}
	return "", errInferNotImplemented
}
