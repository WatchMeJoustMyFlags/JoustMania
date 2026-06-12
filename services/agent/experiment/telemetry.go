package experiment

// Span names + attribute keys for the experiment audit trail (#932, M7-5). Dot
// notation per the repo convention (decision/telemetry.go). The Gate emits one
// span per proposal: accepted experiments get code_improvement.proposed; blocked
// ones get the same span with code_improvement.blocked=true plus the reason, so
// a Jaeger trace shows every proposal and exactly why it was (or was not)
// applied — recorded, never silently dropped (mirrors agent.action's
// decision.blocked contract).
const (
	// SpanProposed wraps one Gate.Review of a Proposal. Named for the AC mapping
	// in #932 ("blocked span" → code_improvement.blocked): the proposed/blocked
	// pair lives on this single span.
	SpanProposed = "code_improvement.proposed"
)

// Custom attribute keys of the experiment span schema (#932).
const (
	// AttrFlagKey is the game flag the proposal experiments on.
	AttrFlagKey = "code_improvement.flag_key"
	// AttrBlocked is true when the Gate rejected the proposal; the write did not
	// happen. False on an accepted (and applied) proposal.
	AttrBlocked = "code_improvement.blocked"
	// AttrReason carries the typed rejection reason when AttrBlocked is true
	// (empty otherwise).
	AttrReason = "code_improvement.reason"
	// AttrRationale records the agent's hypothesis for the audit trail.
	AttrRationale = "code_improvement.rationale"
)

// instrumentationName scopes the experiment tracer (same scope as the rest of
// the agent so all agent spans share one instrumentation library).
const instrumentationName = "github.com/joustmania/agent"
