package decision

// Telemetry constants that change once flagd is wired into the loop (#727).
// The #724 placeholders (flagProviderStub, UnrestrictedAllowed) stay defined in
// telemetry.go for reference; these supersede them on the live decision spans.
const (
	// flagProviderName is the feature_flag.provider.name recorded on the
	// interventions.allowed span event now that the real OpenFeature/flagd
	// provider resolves the flag (replaces the #724 "stub").
	flagProviderName = "flagd"
	// AllowedNone is the interventions.allowed / feature_flag.result.variant
	// rendering when the evaluated allow-list is empty — the fail-closed
	// default dispatches nothing. Distinct from the #724 "unrestricted"
	// placeholder, which meant "no permission information".
	AllowedNone = "none"
	// AttrDecisionBlockReason carries the structured BlockReason (#728) on a
	// blocked decision span (not_allowed / battery_threshold / rate_limit), so
	// #729 finds the attribution on the span as well as in the LayerState.
	AttrDecisionBlockReason = "decision.block_reason"
)
