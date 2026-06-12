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
}

// newEndpointBackend builds a production probe for one tier. An empty addr yields a
// backend that is always unreachable (Available short-circuits false) — the correct
// state for an unconfigured tier (no cloud credentials, no Jetson on the LAN).
func newEndpointBackend(name, addr string) *endpointBackend {
	return &endpointBackend{name: name, addr: addr}
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

// Infer is the PRODUCTION inference call seam (#739) — and is deliberately
// NOT-YET-IMPLEMENTED. The real model call (Ollama /api/chat for the Jetson
// gemma3:4b and localhost phi4-mini tiers, the cloud chat API for claude/copilot)
// is OUT OF SCOPE here: the Jetson backend is hardware-blocked (#738) and the
// cloud backend is credential-blocked (#742). Returning an error keeps the system
// HONEST and SAFE: a tier whose endpoint TCP-dials open (so Available reports it
// reachable, and inference.used names it) but has no client yet cannot fabricate
// a Decision — llm_decide catches this error and falls back to rules with
// FallbackUnparseable-class attribution. #738/#742 replace this body with: build
// the provider request from `prompt` (System+User), POST to e.addr with a bounded
// ctx, and return the model's raw response string for decode.go to parse. The
// signature and the parse pipeline are already in place, so those issues only fill
// in the transport.
func (e *endpointBackend) Infer(_ context.Context, _ llm.Prompt) (string, error) {
	return "", errInferNotImplemented
}
