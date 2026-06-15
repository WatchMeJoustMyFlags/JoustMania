package decision

import (
	"context"
	"net"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/joustmania/agent/llm"
)

// resolver.go is the inference availability checker and fallback chain (#741).
// It answers ONE question per decision cycle, cheaply: given the configured model
// flag, WHICH inference tier would serve this decision right now? It walks a fixed
// chain of tiers from the configured model down to the always-available rules
// engine, returning the first reachable tier — so the agent uses whatever
// intelligence is reachable and degrades gracefully to rules when nothing is:
//
//	configured model (claude | copilot)   -> cloud tier      [agent.model flag]
//	    ↓ unreachable
//	gemma3:4b on Jetson                    -> jetson tier
//	    ↓ unreachable
//	phi4-mini on localhost                 -> localhost tier
//	    ↓ unreachable
//	rules engine                           -> always available; the system ALWAYS decides
//
// CRITICAL — this is the ABSTRACTION + resolver + attribution, NOT the call. There
// is no real backend yet: #739 implements the actual llm_decide call, Jetson #738
// and cloud #742 are hardware/credential-blocked. So a "reachable" tier here means
// "this is who #739 WILL call once it lands" — the rules engine still produces the
// Decision until then. The resolver's job is solely to make inference.used /
// inference.fallback_reason HONEST on every decision span (#741 acceptance #3): it
// reports the tier that WOULD serve, and the fallback_reason explaining any
// degradation, while decide() keeps running the deterministic rules engine.
//
// Coordination with the #847 call gate: the gate decides WHETHER to attempt llm
// (eligibility -> cadence -> budget); the resolver decides WHICH tier would serve.
// decide() runs the gate FIRST and only resolves a backend when the gate ADMITS —
// there is no point resolving a tier for an attempt that will not be made. See
// decision.go's decide() for the exact ordering.
//
// Availability is checked PERIODICALLY and CACHED between checks (#741 acceptance
// #2): a per-decision resolve reads the cache only (a cheap mutex-guarded slice
// read), never a live probe. A background ticker re-probes every tier on an
// interval, so an unplugged tier degrades within one interval and a recovered tier
// is climbed back to within one interval (#741 acceptance #4/#5). This keeps the
// hot decision path off the network entirely while still tracking reality.

// Inference tier identifiers, used as inference.used on the decision span (#741).
// Each names a distinct rung of the fallback chain. The local-model and cloud
// names match the agent.model flag vocabulary so a configured model maps onto its
// tier (resolveTierFromModel). InferenceRules (telemetry.go) is the bottom rung —
// always available — and is reused as the chain's terminal value.
const (
	// TierCloud is the configured cloud model (claude or copilot via the agent.model
	// flag) — the top of the chain. Its reachability is the network reachability of
	// the cloud inference endpoint (#742, credential-blocked today, so unreachable in
	// dev and the chain degrades past it).
	TierCloud = "cloud"
	// TierGemma is gemma3:4b on the Jetson (#738, hardware-blocked today). Named by
	// the model so a `model=gemma3:4b` flag selects it as the chain top.
	TierGemma = "gemma3:4b"
	// TierPhi is phi4-mini on localhost (#739 will run it here first). It is the
	// default-model tier (flags.DefaultModel == "phi4-mini") and the lowest LLM rung
	// above the rules engine.
	TierPhi = "phi4-mini"
)

// cloudModels are the agent.model flag values that name the cloud tier. A model
// flag of "claude" or "copilot" puts the cloud tier at the TOP of the chain; any
// other recognized local model (gemma3:4b, phi4-mini) starts the chain lower, and
// an unrecognized model is treated as the cloud tier (start at the top and let the
// chain degrade) so a typo or a future model name never silently skips tiers.
var cloudModels = map[string]struct{}{
	"claude":  {},
	"copilot": {},
}

// DefaultProbeInterval is how often the resolver re-probes every tier's
// availability in the background (#741). It is the cache TTL: an unplugged tier is
// noticed, and a recovered tier is climbed back to, within at most this interval.
// A const (not a flag) keeps #741 simple — availability cadence is an operational
// constant, not a per-game tunable; main.go may override it via NewResolver's
// interval argument if an env knob is ever wanted. Chosen at 15s as a balance
// between detecting an unplugged Jetson promptly and not hammering endpoints.
const DefaultProbeInterval = 15 * time.Second

// Backend is one rung of the inference fallback chain: a named tier whose
// reachability can be probed. The rules engine is NOT a Backend — it is the
// implicit, always-available terminal rung the resolver returns when every
// Backend is unreachable, so it never needs a probe.
//
// Available is the INJECTION SEAM for tests (#741 acceptance #2/#4/#5): production
// wiring uses a real network probe (endpointBackend), tests use a counting/flippable
// fake so availability is deterministic and no test ever touches the network. It
// takes a context so a real probe can honor a dial timeout / cancellation.
type Backend interface {
	// Name is the tier identifier recorded as inference.used (e.g. "phi4-mini").
	Name() string
	// Available reports whether this tier is reachable right now. It is called
	// ONLY by the resolver's background refresh, never on the decision hot path —
	// the resolver caches the result. A real probe should be cheap and bounded
	// (a short-timeout dial), but even a slow one cannot stall a decision because
	// decisions read the cache.
	Available(ctx context.Context) bool
	// Infer is the actual inference call (#739): it sends the objective-aware
	// prompt (llm.Build, already objective-weighted via the snapshot) to this tier
	// and returns the model's RAW response text, which llm_decide parses
	// DEFENSIVELY into a Decision (unparseable/out-of-vocab output never dispatches
	// — see decode.go). It is called ONLY for a resolved, REACHABLE non-rules tier
	// (the rules engine is the implicit terminal rung and is NOT a Backend), and
	// ONLY after the #847 gate admits, so a tier that returns an error here simply
	// falls back to rules with a recorded reason. Cohesive with Available: a tier
	// reported reachable should be able to Infer; production endpointBackend.Infer
	// is the seam #738 (Jetson) / #742 (cloud) fill in — until then it errors and
	// the chain safely degrades to rules. ctx bounds the call (timeout/cancel).
	Infer(ctx context.Context, prompt llm.Prompt) (string, error)
}

// Resolver holds the ordered inference chain and a periodically-refreshed
// availability cache (#741). It is SHARED across all per-game Loops (one cache for
// the whole agent, like the global llmBudget): main.go constructs ONE Resolver and
// injects it into every Loop via SetResolver, so all concurrent games see the same
// availability picture and a single ticker re-probes for all of them. It is safe
// for concurrent use — resolve() is called from the concurrent Export handler
// goroutines, and the background refresh writes the cache under the same mutex.
type Resolver struct {
	// chain is the ordered tier list, TOP (cloud) to BOTTOM (lowest llm rung). It is
	// immutable after construction; resolve() walks it from the configured-model
	// index downward. The rules engine is the implicit terminal rung below it.
	chain []Backend

	// inferenceTier is the name of the configured-inference-backend tier (#964): when
	// AGENT_INFERENCE_BACKEND=openai, main.go builds a Resolver whose chain TOP is a
	// real OpenAI-compatible tier named after AGENT_INFERENCE_MODEL, probed at the
	// host:port of AGENT_INFERENCE_BASE_URL and Infer-delegating to the same client
	// the proposer uses. When this is non-empty resolve() ALWAYS starts the walk at
	// this tier regardless of the agent.model flag, so the configured backend IS the
	// inference path and a stale/default model flag (phi4-mini) can no longer skip
	// past it down the chain — closing the #964 "resolver decoupled from
	// AGENT_INFERENCE_BASE_URL" gap. Empty (the stub default) restores the pre-#964
	// behavior: the walk starts at the tier the model flag selects (resolveTierFromModel).
	inferenceTier string

	// mu guards available. The decision hot path takes it for a cheap read; the
	// background refresh takes it for the write. Held only around the map read and
	// write, never across a probe (probes run before the lock is taken).
	mu sync.Mutex
	// available is the cached reachability per tier name, refreshed by the
	// background ticker (refresh) or an explicit Refresh. A tier absent from the map
	// (before the first refresh) reads as unreachable — fail-safe: until we have
	// probed, assume nothing is reachable and serve from rules.
	available map[string]bool

	interval time.Duration
	now      func() time.Time
}

// NewResolver builds a Resolver over the given ordered chain (top tier first) with
// the given background re-probe interval (DefaultProbeInterval when non-positive).
// The cache starts EMPTY — every tier reads as unreachable until the first refresh,
// so a resolve() before Start (or before the first tick) honestly bottoms out at
// rules rather than optimistically claiming a tier is up. Call Start to begin the
// background refresh, or Refresh once synchronously (tests do the latter for
// determinism).
func NewResolver(chain []Backend, interval time.Duration) *Resolver {
	if interval <= 0 {
		interval = DefaultProbeInterval
	}
	return &Resolver{
		chain:     chain,
		available: make(map[string]bool),
		interval:  interval,
		now:       time.Now,
	}
}

// DefaultChain is the production inference chain (#741), top tier first: cloud
// (claude/copilot, #742), then gemma3:4b on the Jetson (#738), then phi4-mini on
// localhost (#739). Each tier probes a TCP endpoint; in dev every endpoint is
// unreachable, so the chain degrades to rules — which is exactly the standalone,
// no-Jetson-no-cloud behavior #741 requires. main.go reads the endpoint addresses
// from env (sensible defaults documented on the Endpoints fields). Every tier's
// Infer returns the not-implemented sentinel (no real client) — use
// DefaultChainWithInfer to wire a real OpenAI-compatible backend (#1048).
func DefaultChain(eps Endpoints) []Backend {
	return DefaultChainWithInfer(eps, nil)
}

// DefaultChainWithInfer is DefaultChain with a real inference delegate injected into
// EVERY tier (#1048). When inferFn is non-nil (AGENT_INFERENCE_BACKEND=openai,
// main.go passing the shared inference.OpenAIBackend.Infer), a resolved+reachable
// tier calls the real OpenAI-compatible client instead of returning the sentinel —
// the SAME client instance the proposer uses, so one backend feeds both seams. A
// nil inferFn yields the exact DefaultChain behavior (sentinel error → rules), so
// the stub default is unchanged. Availability is still the per-tier TCP dial; the
// delegate only changes what a reachable tier returns from Infer.
func DefaultChainWithInfer(eps Endpoints, inferFn func(ctx context.Context, prompt llm.Prompt) (string, error)) []Backend {
	return []Backend{
		newEndpointBackendWithInfer(TierCloud, eps.Cloud, inferFn),
		newEndpointBackendWithInfer(TierGemma, eps.Jetson, inferFn),
		newEndpointBackendWithInfer(TierPhi, eps.Localhost, inferFn),
	}
}

// InferenceTier describes the configured real inference backend as a resolver tier
// (#964). When AGENT_INFERENCE_BACKEND=openai, main.go fills it from the SAME
// AGENT_INFERENCE_* config that builds the OpenAIBackend, so the resolver's
// availability probe targets the configured backend's host:port (Addr) and the tier
// is named after the configured model (Model = AGENT_INFERENCE_MODEL). The zero value
// (no real backend selected) yields no inference tier and the chain/resolver behave
// exactly as before — the stub default is byte-identical.
type InferenceTier struct {
	// Model is the tier name, equal to AGENT_INFERENCE_MODEL. It becomes inference.used
	// on the decision span and gen_ai.request.model on the inference span, so a single
	// AGENT_INFERENCE_MODEL drives the backend request AND the decision-loop attribution
	// (#964 gap 3: one source of truth, no more phi4-mini/AGENT_INFERENCE_MODEL drift).
	Model string
	// Addr is the host:port the availability probe TCP-dials, parsed from
	// AGENT_INFERENCE_BASE_URL (see InferenceProbeAddr). Empty makes the tier
	// permanently unreachable (the chain degrades exactly as today), so a
	// mis-/un-parseable base URL fails SAFE to rules rather than to a broken tier.
	Addr string
	// Infer is the real OpenAI-compatible delegate (the SAME inference.OpenAIBackend
	// the proposer uses). Non-nil on AGENT_INFERENCE_BACKEND=openai; a reachable tier
	// then produces real model text instead of the not-implemented sentinel.
	Infer func(ctx context.Context, prompt llm.Prompt) (string, error)
}

// NewInferenceResolver builds the production Resolver for the configured real
// inference backend (#964). The configured-inference tier (named after the model) is
// the chain TOP, probed at its own host:port, followed by the legacy cloud/jetson/
// localhost tiers (still probed at their endpoints, still Infer-delegating to the
// same client) so the existing fallback chain remains intact below the configured
// backend. resolve() ALWAYS starts the walk at the configured-inference tier (it is
// the canonical inference path), then degrades down the legacy chain and finally to
// rules — graceful degradation is preserved end to end. interval is the background
// re-probe cadence (DefaultProbeInterval when non-positive).
func NewInferenceResolver(tier InferenceTier, eps Endpoints, interval time.Duration) *Resolver {
	chain := make([]Backend, 0, 4)
	chain = append(chain, newEndpointBackendWithInfer(tier.Model, tier.Addr, tier.Infer))
	chain = append(chain, DefaultChainWithInfer(eps, tier.Infer)...)
	r := NewResolver(chain, interval)
	r.inferenceTier = tier.Model
	return r
}

// InferenceProbeAddr derives the host:port a TCP availability probe should dial from
// an OpenAI-compatible base URL (#964). It strips the scheme and any path (e.g.
// "http://192.168.224.1:11434/v1" -> "192.168.224.1:11434"), and when the URL omits a
// port it supplies the scheme default (443 for https, else 80) so the probe still has
// a concrete dial target. An unparseable / host-less URL returns "" — the caller then
// builds a permanently-unreachable tier (fail-safe to rules), never a panic.
func InferenceProbeAddr(baseURL string) string {
	u, err := url.Parse(strings.TrimSpace(baseURL))
	if err != nil || u.Host == "" {
		return ""
	}
	host := u.Hostname()
	if host == "" {
		return ""
	}
	port := u.Port()
	if port == "" {
		if u.Scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	}
	return net.JoinHostPort(host, port)
}

// resolveTierFromModel maps the configured agent.model flag to the index of the
// chain tier it selects as the TOP of the walk. claude/copilot -> the cloud tier
// (index 0); gemma3:4b -> the Jetson tier; phi4-mini -> the localhost tier; any
// unrecognized model -> the top (cloud), so a future/misspelled model starts high
// and degrades through every rung rather than silently skipping the chain. It
// returns the chain index to start resolving from.
func (r *Resolver) resolveTierFromModel(model string) int {
	// #964: when a real inference backend is configured (AGENT_INFERENCE_BACKEND=openai),
	// it is the canonical inference path — ALWAYS start the walk at its tier, whatever
	// the agent.model flag says. This is what wires the decision-loop resolver to
	// AGENT_INFERENCE_BASE_URL/AGENT_INFERENCE_MODEL: the configured backend's tier is
	// the chain top, so a reachable backend resolves to a non-nil Backend and decide()
	// actually CALLS it (mode=llm) instead of degrading to rules because a default
	// phi4-mini flag pointed at an unreachable localhost tier.
	if r.inferenceTier != "" {
		for i, b := range r.chain {
			if b.Name() == r.inferenceTier {
				return i
			}
		}
	}
	if _, isCloud := cloudModels[model]; isCloud {
		return 0
	}
	// A local model name selects its own tier as the chain top. Match by the tier's
	// Name(), which equals the model string for the local rungs (gemma3:4b, phi4-mini).
	for i, b := range r.chain {
		if b.Name() == model {
			return i
		}
	}
	// Unrecognized: start at the top and let the chain degrade.
	return 0
}

// Resolution is the outcome of one resolve() call (#741): the tier that would
// serve this decision and, when it is NOT the configured tier, why it degraded.
type Resolution struct {
	// Used is the resolved tier name recorded as inference.used: a tier from the
	// chain when one at/below the configured model is reachable, else InferenceRules
	// when the whole chain is unreachable and it bottoms out at the rules engine.
	Used string
	// Backend is the resolved tier's Backend — the one llm_decide (#739) calls
	// Infer on — or NIL when the chain bottomed out at the rules engine (Used ==
	// InferenceRules). decide() branches on this: a non-nil Backend means a real
	// llm tier is reachable, so the LLM path runs; nil means rules decide (the
	// #741 fallback). Carrying the instance (not just the name) avoids a second
	// chain walk to recover it. The rules engine is NOT a Backend, hence nil.
	Backend Backend
	// FallbackReason is inference.fallback_reason: "" when the configured tier
	// itself is reachable (no degradation), FallbackEndpointUnreachable when a LOWER
	// tier serves (a higher one was unreachable), or FallbackNoBackend when the whole
	// chain is unreachable and only rules remain. It composes with the #847 gate
	// reason in decide(): the gate reason wins when the gate DENIED; this reason is
	// only consulted on an ADMITTED cycle.
	FallbackReason string
}

// resolve walks the chain from the configured model's tier downward and returns
// the first reachable tier, reading the cache ONLY (no probe — #741 acceptance #2,
// the hot path never touches the network). When the configured tier is reachable
// the resolution carries no fallback_reason; when a LOWER tier serves the reason is
// endpoint_unreachable (a higher tier was down); when the whole chain is unreachable
// it bottoms out at the rules engine with no_backend_available. Concurrency-safe:
// the cache read is mutex-guarded.
func (r *Resolver) resolve(configuredModel string) Resolution {
	start := r.resolveTierFromModel(configuredModel)

	r.mu.Lock()
	defer r.mu.Unlock()
	for i := start; i < len(r.chain); i++ {
		if r.available[r.chain[i].Name()] {
			reason := ""
			// A tier BELOW the configured one means a higher tier was unreachable —
			// honestly attribute the degradation. The configured tier serving itself
			// carries no reason (configured == used).
			if i > start {
				reason = FallbackEndpointUnreachable
			}
			// Carry the resolved Backend so decide() can call Infer without a second
			// chain walk; a non-nil Backend is the signal that a real llm tier serves.
			return Resolution{Used: r.chain[i].Name(), Backend: r.chain[i], FallbackReason: reason}
		}
	}
	// Whole chain unreachable: the rules engine is the always-available terminal
	// rung. no_backend_available reconciles with #847's existing use of the same
	// constant for the gate-admitted-but-no-backend case — both mean "the llm path
	// was wanted but nothing answered, so rules decided".
	return Resolution{Used: InferenceRules, FallbackReason: FallbackNoBackend}
}

// Refresh probes every tier ONCE and updates the cache (#741). The background
// ticker calls it on each tick; tests call it directly for deterministic, single-
// threaded refreshes (no goroutine, no clock). Probes run OUTSIDE the lock so a
// slow/blocking probe never stalls a concurrent resolve() on the hot path — only
// the final cache write takes the mutex. ctx bounds the probes (cancellation /
// dial timeout); a cancelled ctx still writes whatever results completed.
func (r *Resolver) Refresh(ctx context.Context) {
	next := make(map[string]bool, len(r.chain))
	for _, b := range r.chain {
		next[b.Name()] = b.Available(ctx)
	}
	r.mu.Lock()
	r.available = next
	r.mu.Unlock()
}

// Start launches the background re-probe loop and returns immediately (#741). The
// FIRST probe runs INSIDE the goroutine, not synchronously, so agent startup is
// never blocked waiting on optional inference endpoints: a black-holed Jetson or
// cloud endpoint would otherwise stall the server's readiness by up to the dial
// timeout per tier. Until that first probe completes the cache is empty and
// resolve() serves from rules — the fail-safe floor — which is harmless here:
// nothing consults the resolver before then (the #847 cadence floor delays the
// first LLM attempt, and #739, the actual call, is not wired yet), so the brief
// rules window costs nothing. After priming it re-probes every interval until ctx
// is cancelled. main.go calls this once on the shared Resolver during startup; it
// must not be called twice on the same Resolver.
func (r *Resolver) Start(ctx context.Context) {
	go func() {
		r.Refresh(ctx) // prime the cache in the background, off the startup path
		ticker := time.NewTicker(r.interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				r.Refresh(ctx)
			}
		}
	}()
}
