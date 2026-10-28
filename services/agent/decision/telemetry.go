package decision

// Async discard reasons (#917, reused by the remote decide path #1226). When a
// remote decide answer lands it is re-validated against the CURRENT game context
// (fetched fresh at landing, NOT the fire-time snapshot — the game may have moved
// on while the call was in flight). An answer that fails a check is dropped and the
// reason rides agent.decide as decide.discarded_reason.
const (
	// DiscardStaleContext: the partition that fired the inference no longer exists
	// at apply time (the game ended past grace and was evicted, or never reappeared)
	// — there is no current context to re-validate against, so the result is stale.
	DiscardStaleContext = "stale_context"
	// DiscardGameEnded: the partition still exists but the game is no longer active
	// (GameActive flipped false while inference was in flight). An intervention into
	// an ended game is meaningless; drop it.
	DiscardGameEnded = "game_ended"
	// DiscardTargetGone: the decision targeted a specific player who is no longer
	// alive/connected at apply time (eliminated or disconnected during inference).
	// A session-scoped decision (empty target) never hits this.
	DiscardTargetGone = "target_gone"
	// DiscardStateTooOld: the game state the remote request was built on (the
	// fire-time GameContext.UpdatedAt) was older than decision.max_state_age_ms
	// when the answer landed (#2123): the answer describes a game that has moved on.
	DiscardStateTooOld = "state_too_old"
)

// decision.not_applied_reason values of a write that was not applied.
const (
	// NotAppliedWriteGateOff is the decision.not_applied_reason of a write the
	// interventions_enabled gate dropped.
	NotAppliedWriteGateOff = "write_gate_off"
	// NotAppliedWriteFailed is the reason of a write that returned an error (A27).
	NotAppliedWriteFailed = "write_failed"
)

// Placeholder values emitted until the subsystems behind them are built. The
// schema is always complete on every decision span so the audit trace shows
// its full shape from day one; later issues replace placeholders with real
// values.
const (
	// AgentName identifies this agent (gen_ai.agent.name).
	AgentName = "joustmania-agent"
	// DefaultInference is inference.used on a capture-only agent.llm.retro span (no
	// text backend wired): no inference of any kind ran.
	DefaultInference = "none"
	// FallbackNoBackend is inference.fallback_reason on a capture-only retro span.
	FallbackNoBackend = "no_backend_available"
	// FallbackAgentDisabled is inference.fallback_reason on a capture-only retro
	// span when the agent `enabled` kill switch is off at game end (#1270): a text
	// backend is wired, but "agent off" means no model call and no budget slot.
	FallbackAgentDisabled = "agent_disabled"
	// LLM call-gate reasons (#847), on agent_llm_gated_total and as the remote
	// decide path's decide.fallback_reason:
	//
	// FallbackNotEligible: the game's kind is not in llm.eligible_game_kinds (e.g.
	// a shadow game under the default ["real"] list) — the remote backend is not
	// called and rules decide.
	FallbackNotEligible = "llm_not_eligible"
	// FallbackBudgetExhausted: the global per-minute request budget
	// (llm.max_requests_per_minute, shared across all games and the retro) is full
	// this window.
	FallbackBudgetExhausted = "llm_budget_exhausted"
	// DefaultObjectives until the objectives flag schema exists (#725).
	DefaultObjectives = "unset"
	// UnrestrictedAllowed is the interventions.allowed summary while the
	// Permissions stub reports no restriction information (#725).
	UnrestrictedAllowed = "unrestricted"
	// flagProviderStub names the feature_flag.provider.name until flagd is
	// wired up (#725).
	flagProviderStub = "stub"
	// interventionsAllowedFlagKey is the feature flag the Permissions source
	// represents (#725 flag schema).
	interventionsAllowedFlagKey = "interventions.allowed"
)

// instrumentationName scopes the agent's tracer.
const instrumentationName = "github.com/joustmania/agent"
