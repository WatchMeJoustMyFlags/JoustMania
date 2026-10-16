package decision

import (
	"sync"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// decide_registry.go maps a decision.backend flag value to a factory that builds
// the decide.Backend for it (#1226). There is no gateway: the agent calls the
// selected backend directly.
//
// A factory runs on EVERY remote decide call with the cycle's flag snapshot, so a
// change to a backend's model / base URL / timeout flag applies on the very next
// call with no restart. Secrets (API keys) are not flags: main reads them from the
// environment once at startup and the factories close over them, so they never
// appear in a flag file or a span. The claude / openai / jev registrations live in
// services/agent/decide_backends.go (newDecideRegistry, #1244).
//
// `rules` is NOT registered here: it is the in-process floor the loop runs itself
// (ObjectiveRules.EvaluateDecide), so it can never be unknown or unavailable.

// DecideBackendFactory builds the decide.Backend for one decide call from the
// current decision-backend flags (plus env secrets the factory reads itself). It
// may return nil when the backend cannot be built. It runs on every eligible
// decision cycle (before the cadence gate), so it must be cheap: build a config
// struct, no I/O. A backend that also implements
// Available() bool (the convention for decide/backends/<name>) and reports false
// is treated as unavailable; either way the decision falls back to rules.
type DecideBackendFactory func(cfg flags.DecisionBackend) decide.Backend

// availability is the optional interface remote backends implement (false when a
// key or model is missing).
type availability interface {
	Available() bool
}

// Fallback reasons recorded as decide.fallback_reason when a remote backend was
// requested but the rules engine produced the decision.
const (
	// DecideFallbackUnknownBackend: decision.backend names no registered backend.
	DecideFallbackUnknownBackend = "unknown_backend"
	// DecideFallbackUnavailable: the backend is registered but cannot serve (no
	// API key / no model configured, or its factory returned nil).
	DecideFallbackUnavailable = "backend_unavailable"
	// DecideFallbackTimeout: the backend did not answer within its timeout flag.
	DecideFallbackTimeout = "timeout"
	// DecideFallbackError: the backend returned an error (transport, HTTP, parse).
	DecideFallbackError = "error"
	// DecideFallbackInvalidAnswer: the response failed decide.Validate.
	DecideFallbackInvalidAnswer = "invalid_answer"
	// DecideFallbackNotWired: the loop has no async wiring (no ContextProvider), so
	// a remote call cannot run off the decision loop. Production always wires it.
	DecideFallbackNotWired = "async_not_wired"
)

// DecideRegistry is the name -> factory table for remote decision backends. Safe
// for concurrent use; one instance is shared by every per-game loop.
type DecideRegistry struct {
	mu        sync.RWMutex
	factories map[string]DecideBackendFactory
}

// NewDecideRegistry returns an empty registry (only the built-in rules floor).
func NewDecideRegistry() *DecideRegistry {
	return &DecideRegistry{factories: map[string]DecideBackendFactory{}}
}

// Register maps name (a decision.backend value) to f. Registering "rules" or a
// nil factory is ignored: rules is the built-in floor.
func (r *DecideRegistry) Register(name string, f DecideBackendFactory) {
	if r == nil || f == nil || name == "" || name == flags.DecisionBackendRules {
		return
	}
	r.mu.Lock()
	r.factories[name] = f
	r.mu.Unlock()
}

// Resolve builds the backend cfg.Backend selects. It returns the backend and ""
// when it can serve, or nil and the fallback reason (unknown_backend /
// backend_unavailable) when the decision must fall back to rules. A nil registry
// knows no backends. Resolve must not be called for the rules backend.
func (r *DecideRegistry) Resolve(cfg flags.DecisionBackend) (decide.Backend, string) {
	if r == nil {
		return nil, DecideFallbackUnknownBackend
	}
	r.mu.RLock()
	f, ok := r.factories[cfg.Backend]
	r.mu.RUnlock()
	if !ok {
		return nil, DecideFallbackUnknownBackend
	}
	b := f(cfg)
	if b == nil {
		return nil, DecideFallbackUnavailable
	}
	if a, ok := b.(availability); ok && !a.Available() {
		return nil, DecideFallbackUnavailable
	}
	return b, ""
}
