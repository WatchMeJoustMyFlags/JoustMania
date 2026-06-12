package flags

import (
	"context"
	"log/slog"
	"sync/atomic"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// LifecycleHolder is the concurrency-safe, hot-reloadable home of the four
// calibration values (#927): lifecycle.player_ttl_seconds,
// lifecycle.session_grace_seconds, lifecycle.evict_interval_seconds, and
// decision.throttle_seconds.
//
// History: #766 F5 read these four ONCE at startup and baked them into the store
// TTLs, the eviction ticker, and the decision-loop throttle, so retuning the
// agent's perception/cadence required a restart (#927). #927 makes them LIVE via
// the OpenFeature provider's configuration-change event — the mechanism the
// maintainer asked for ("we can utilize the openfeature listener for
// configuration changed for this"): flagd emits a config-change when its flag
// source changes, the Go SDK surfaces it as openfeature.ProviderConfigChange, and
// the handler re-evaluates the four flags and Stores them here. The consumers
// (the decision Loop's throttle, main.go's eviction ticker, and the gamecontext
// Store's eviction TTLs) read the CURRENT value from this holder on their own
// hot path instead of a construction-time constant — so a flag change takes
// effect with no restart and WITHOUT polling the flag client per decision cycle
// (the config-change event is the only thing that re-reads flagd; reads here are
// a lock-free atomic load).
//
// Concurrency: the handler Reload()s from the SDK's event goroutine while the
// Loop / ticker / Store read from their own goroutines, so the value is held in
// an atomic.Pointer — a lock-free load on the read (hot) path and a single
// atomic store on the (rare) config-change path. The four other agent flag
// layers (enabled/mode/objectives/policy/fitness/llm) are unaffected: they were
// already re-evaluated per cycle (#727) and must NOT route through this holder.
type LifecycleHolder struct {
	// flags is the source the config-change handler re-evaluates the four flags
	// from. It is the same *Flags the per-cycle layers use (one flagd client).
	flags *Flags
	log   *slog.Logger
	// val holds the most recently evaluated Lifecycle. atomic.Pointer gives the
	// hot-path readers a lock-free load and the config-change handler a single
	// atomic store; it is never nil after NewLifecycleHolder primes it.
	val atomic.Pointer[Lifecycle]
}

// NewLifecycleHolder builds a holder and PRIMES it once with the initial
// Lifecycle read, so every consumer sees correct values before any
// configuration-change event arrives (and even if one never does — e.g. flagd
// already at its final config at startup). The initial read keeps the existing
// safe-default / non-positive guards: a down control plane primes the holder
// with the flagd-schema defaults (the former hardcoded constants), exactly as
// the read-at-startup path did. log may be nil (slog.Default() is used).
func NewLifecycleHolder(ctx context.Context, f *Flags, log *slog.Logger) *LifecycleHolder {
	if log == nil {
		log = slog.Default()
	}
	h := &LifecycleHolder{flags: f, log: log}
	initial := f.Lifecycle(ctx)
	h.val.Store(&initial)
	return h
}

// Load returns the current Lifecycle values (lock-free atomic load). Safe to call
// from any goroutine concurrently with Reload.
func (h *LifecycleHolder) Load() Lifecycle {
	return *h.val.Load()
}

// Reload re-evaluates the four calibration flags and atomically replaces the held
// values. It is the function the OpenFeature configuration-change handler calls
// (see provider.go), and it is exported so tests can drive the update path
// deterministically WITHOUT a live flagd — exactly as #927's acceptance test does
// (a settable fake flag source + a direct Reload stands in for the event).
//
// A re-eval of four flags is cheap and must not block the SDK's event goroutine;
// each flag still falls back to its safe default on any error (Flags.Lifecycle's
// guards), so a transient flagd hiccup during a config-change reverts a value to
// its schema default rather than to zero. The ctx is the evaluation context for
// the four flag reads.
func (h *LifecycleHolder) Reload(ctx context.Context) {
	next := h.flags.Lifecycle(ctx)
	prev := *h.val.Load()
	h.val.Store(&next)
	// Log only the deltas so a config-change for an unrelated agent flag (the
	// event fires for ANY flag in the domain) is near-silent, while an actual
	// calibration retune is visible in the agent log for the demo (#743).
	if next != prev {
		h.log.Info("agent lifecycle flags hot-reloaded via OpenFeature config-change (#927)",
			"player_ttl", next.PlayerTTL,
			"session_grace", next.SessionGrace,
			"evict_interval", next.EvictInterval,
			"decision_throttle", next.DecisionThrottle,
		)
	}
}

// PlayerTTL / SessionGrace / EvictInterval / DecisionThrottle are the live source
// funcs the consumers hold instead of a fixed duration. Each is a closure-free
// method value (h.PlayerTTL) the consumer reads on its hot path: the gamecontext
// Store reads PlayerTTL/SessionGrace at eviction time, main.go's ticker reads
// EvictInterval each loop, and the decision Loop reads DecisionThrottle in
// shouldLog(). They never block and never touch flagd — just an atomic load.

// PlayerTTL returns the current lifecycle.player_ttl_seconds as a duration.
func (h *LifecycleHolder) PlayerTTL() time.Duration { return h.Load().PlayerTTL }

// SessionGrace returns the current lifecycle.session_grace_seconds as a duration.
func (h *LifecycleHolder) SessionGrace() time.Duration { return h.Load().SessionGrace }

// EvictInterval returns the current lifecycle.evict_interval_seconds as a duration.
func (h *LifecycleHolder) EvictInterval() time.Duration { return h.Load().EvictInterval }

// DecisionThrottle returns the current decision.throttle_seconds as a duration.
func (h *LifecycleHolder) DecisionThrottle() time.Duration { return h.Load().DecisionThrottle }

// RegisterConfigChangeHandler wires the OpenFeature configuration-change event to
// this holder's Reload (#927, the maintainer's requested mechanism). It returns
// the registered openfeature.EventCallback so the caller can RemoveHandler at
// shutdown if it wishes (openfeature.Shutdown also clears all handlers).
//
// We register a DOMAIN-scoped handler on the agent client (client.AddHandler)
// rather than the API-global openfeature.AddHandler, so the agent only reacts to
// its OWN flagd domain's config-changes — not another provider's, were one ever
// registered. The flagd RPC resolver (provider.go) connects to flagd's evaluation
// port and the provider surfaces flagd's change notifications as
// ProviderConfigChange events on this domain. The event fires for ANY flag in the
// domain (config-changes are rare), and Reload simply re-reads the four it owns.
//
// eventClient is the narrow interface the agent client satisfies (*openfeature.Client);
// taking the interface keeps this unit-testable with a fake that records the
// callback and lets a test invoke it with a synthetic EventDetails.
func (h *LifecycleHolder) RegisterConfigChangeHandler(ctx context.Context, eventClient EventClient) openfeature.EventCallback {
	cb := func(_ openfeature.EventDetails) {
		// Re-evaluate on the event goroutine. Cheap (four flag reads, all with
		// safe-default guards) so it never wedges the SDK's event dispatch.
		h.Reload(ctx)
	}
	// EventCallback is *func(EventDetails); take the address of the local so the
	// SDK stores a stable pointer (RemoveHandler matches on pointer identity).
	callback := openfeature.EventCallback(&cb)
	eventClient.AddHandler(openfeature.ProviderConfigChange, callback)
	return callback
}

// EventClient is the subset of *openfeature.Client the holder needs to register a
// domain-scoped configuration-change handler. The real OpenFeature client
// satisfies it; tests supply a fake that captures the callback.
type EventClient interface {
	AddHandler(eventType openfeature.EventType, callback openfeature.EventCallback)
}
