package decision

// Span names for the agent's decision audit trail (issue #724). The hierarchy
// is always parent -> child:
//
//	agent.span_received          one per triggering OTLP Export
//	  └─ agent.decision          one per Decision the rules engine returns
//	       └─ agent.action       one per decision, wrapping the ActionSink call
//
// Traces are only emitted when the rules engine returns at least one decision
// (including decisions that end up blocked) — the trace IS the audit log of
// agent activity, not of agent idle time.
//
// Where OpenTelemetry semantic conventions exist they are used (semconv
// v1.34.0): rpc.* on agent.span_received (it wraps an inbound OTLP gRPC
// Export), gen_ai.agent.name as the agent identity on agent.decision (the full
// GenAI agent conventions apply once an LLM inference path exists — its
// requireds, e.g. gen_ai.provider.name, are only honest in llm mode), a
// feature_flag.* span event for the interventions.allowed evaluation (the same
// shape the openfeature-hooks-opentelemetry package emits in the Python
// services), and error.type + span status on action failures. The decision.*,
// fitness.*, agent.mode, agent.objectives and interventions.allowed attributes
// have no semantic convention and are custom to this project.
const (
	SpanReceived = "agent.span_received"
	SpanDecision = "agent.decision"
	SpanAction   = "agent.action"
)

// SpanDisabled is the kill-switch trace emitted when the existence layer reports
// the agent off (enabled=false). It is a single agent.decision span (no action
// child — nothing was decided) carrying the existence + capability attribution
// lifted from the disabled-path LayerState, so a Jaeger trace shows "agent off"
// with the flags that were in effect (#729). It is rate-limited to one per
// throttleInterval so a disabled agent under heavy signal load does not flood
// the trace backend.
const SpanDisabled = "agent.disabled"

// Custom attribute keys of the decision-span schema (issue #724). Attributes
// covered by a semantic convention (gen_ai.agent.name, rpc.*, error.type,
// feature_flag.*) use the semconv constants directly and are not listed here.
const (
	// AttrEnabled is the existence-layer kill switch (agent.enabled). Lifted from
	// the cycle's LayerState onto every decision span (including the disabled
	// kill-switch span) so a trace shows whether the agent was live (#729).
	AttrEnabled = "agent.enabled"
	// AttrMode is the inference mode driving decisions: "rules" or "llm".
	AttrMode = "agent.mode"
	// AttrObjectives is the objective weights being pursued (#725/#731).
	AttrObjectives = "agent.objectives"
	// AttrModel / AttrPromptVariant are the capability-layer selections recorded
	// on every decision span for the M4 LLM path (#728/#729).
	AttrModel         = "agent.model"
	AttrPromptVariant = "agent.prompt_variant"
	// AttrPolicyBatteryThreshold / AttrPolicyMovementVarianceWindow /
	// AttrPolicyMaxPerMinute lift the three numeric permission-layer policy flags
	// onto the decision span so a single trace shows the policy in effect (#729).
	AttrPolicyBatteryThreshold       = "policy.battery_threshold"
	AttrPolicyMovementVarianceWindow = "policy.movement_variance_window"
	AttrPolicyMaxPerMinute           = "policy.max_interventions_per_minute"
	// AttrInterventionsAllowed summarizes the permission list in effect. The
	// individual flag evaluation is additionally recorded as a feature_flag.*
	// span event (semconv) on the decision span.
	AttrInterventionsAllowed = "interventions.allowed"
	// AttrInferenceConfigured / AttrInferenceUsed / AttrInferenceFallback
	// describe the inference backend (LLM backend issue). When an LLM call
	// actually happens it is recorded as a gen_ai.* child span per the GenAI
	// semantic conventions; these attributes summarize the outcome.
	AttrInferenceConfigured = "inference.configured"
	AttrInferenceUsed       = "inference.used"
	AttrInferenceFallback   = "inference.fallback_reason"
	// AttrDecisionAction / AttrDecisionReason / AttrDecisionObjective describe
	// the single decision this span audits.
	AttrDecisionAction    = "decision.action"
	AttrDecisionReason    = "decision.reason"
	AttrDecisionObjective = "decision.objective_served"
	// AttrDecisionBlocked is true when the chosen action is not in
	// interventions.allowed. Blocked actions are recorded, never silently
	// dropped: the agent.action span still exists, carrying this attribute.
	AttrDecisionBlocked = "decision.blocked"
	// AttrFitnessEvaluated lists the fitness values evaluated for the
	// decision (#731). Empty until fitness functions exist.
	AttrFitnessEvaluated = "fitness.evaluated"
)

// Placeholder values emitted until the subsystems behind them are built. The
// schema is always complete on every decision span so the audit trace shows
// its full shape from day one; later issues replace placeholders with real
// values.
const (
	// AgentName identifies this agent (gen_ai.agent.name).
	AgentName = "joustmania-agent"
	// DefaultMode until an LLM mode exists.
	DefaultMode = "rules"
	// DefaultInference is the #724 placeholder, retained for reference. #729
	// supersedes inference.configured/used/fallback_reason with honest values:
	// configured = the capability model flag, used = InferenceRules (what ran),
	// fallback_reason = FallbackLLMNotImplemented when mode=llm fell back.
	DefaultInference = "none"
	// InferenceRules is inference.used on every cycle until the M4 LLM path
	// runs: the deterministic rules engine is what actually decided.
	InferenceRules = "rules"
	// FallbackLLMNotImplemented is inference.fallback_reason when mode=llm is
	// selected but the M4 LLM path does not exist and the loop falls back to the
	// rules engine. Empty when not applicable (mode=rules).
	FallbackLLMNotImplemented = "llm_path_not_implemented"
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
