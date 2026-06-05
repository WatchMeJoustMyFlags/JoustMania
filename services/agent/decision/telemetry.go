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

// Custom attribute keys of the decision-span schema (issue #724). Attributes
// covered by a semantic convention (gen_ai.agent.name, rpc.*, error.type,
// feature_flag.*) use the semconv constants directly and are not listed here.
const (
	// AttrMode is the inference mode driving decisions: "rules" or "llm".
	AttrMode = "agent.mode"
	// AttrObjectives is the objective weights being pursued (#725/#731).
	AttrObjectives = "agent.objectives"
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
	// DefaultInference until an inference backend exists.
	DefaultInference = "none"
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
