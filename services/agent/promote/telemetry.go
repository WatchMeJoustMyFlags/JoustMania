package promote

// Span names + attribute keys for the M7-8 PROMOTION flow (#936). Dot notation
// matches the repo span convention (decision/telemetry.go, experiment/*.go).
//
// Where promotion sits in the #931/#932 self-improvement track:
//
//	M7-4/M7-5 (experiment/gate.go, writer.go): the agent forms a structured
//	{flag, value} Proposal; the Writer/Gate apply it to game.json SHADOW-scoped
//	(game_kind != "real") — real games untouched by construction.
//	    ↓
//	M7-7 (experiment/validate.go, #935): run the shadow experiment through the
//	synthetic suite, measure fitness against a real-game baseline, and DECIDE —
//	PROMOTE / DISCARD / REVERT. M7-7 returns the verdict ONLY; it does NOT change
//	the real defaultVariant.
//	    ↓
//	M7-8 (THIS package, #936): take a PROMOTE verdict + its fitness evidence and
//	ACT, per two live flags — open a GitHub issue (hypothesis), open a PR
//	(structured fitness evidence), or apply autonomously to the REAL default +
//	watch + revert. This is the consequential, real-affecting piece, so it is
//	behind the SAFETY RAIL (see promote.go / NewGitHubClientFromEnv).
//
// The promotion is its own trace node so a Jaeger trace shows what the agent DID
// with a validated experiment (the #936 AC: "Outcome visible in traces").
const (
	// SpanPromote wraps one Promoter.Promote of a validated experiment. It carries
	// the resolved gate (mode) + target + the final outcome, so a single span answers
	// "what did the agent do with this promotion, and where". It is the M7-8 node
	// under the M7-7 synthetic_validation tree.
	SpanPromote = "code_improvement.promote"
)

// Attribute keys of the promotion span schema (#936 AC: gate/target/outcome on
// the trace). These intentionally do NOT collide with experiment.Attr* (the
// VALIDATE-gate keys live in experiment/validate_telemetry.go) — distinct phases.
const (
	// AttrGate is the resolved code_improvement.mode the promotion acted under:
	// issue | pr | autonomous. The #936 AC names this "code_improvement.gate" — the
	// runtime "mode" flag is the gate that selects the promotion behaviour. The
	// single attribute a dashboard groups by to see how a promotion was handled.
	AttrGate = "code_improvement.gate"
	// AttrTarget is the resolved code_improvement.target: local | github. Records
	// WHERE a pr/commit landed (github → the GitHub API; local → a local apply).
	AttrTarget = "code_improvement.target"
	// AttrOutcome is the promotion result: applied | discarded | reverted (#936 AC:
	// "code_improvement.outcome"). Distinct from experiment.AttrOutcome's
	// promote/discard/revert verdict — THIS records what the ACTION achieved.
	AttrOutcome = "code_improvement.outcome"
	// AttrFlagKey is the game flag the promoted experiment concerns (echoed from the
	// Proposal so the promotion span is self-describing without the validation span).
	AttrFlagKey = "code_improvement.flag_key"
	// AttrReason carries why a promotion degraded to a no-op / could not act
	// (e.g. "github target requested but real client not enabled", "no real-default
	// writer wired"). Present only when the outcome is not the requested action —
	// the promotion is RECORDED, never silently dropped (mirrors the Gate's blocked
	// contract). Its presence on a github-target span is the SAFETY RAIL's visible
	// proof that an absent env gate degraded to an inert recorded no-op.
	AttrReason = "code_improvement.reason"
	// AttrURL is the issue/PR URL a github-target promotion opened (empty for a
	// local apply or a no-op). On the span so a trace links straight to the artifact.
	AttrURL = "code_improvement.url"
	// AttrFitnessBefore / AttrFitnessAfter / AttrFitnessDelta echo the M7-7 evidence
	// onto the promotion span so the trace shows what justified the action without a
	// cross-span join (same keys as experiment/validate_telemetry.go by design —
	// they describe the SAME fitness numbers, now carried on the promote node).
	AttrFitnessBefore = "code_improvement.fitness_before"
	AttrFitnessAfter  = "code_improvement.fitness_after"
	AttrFitnessDelta  = "code_improvement.fitness_delta"
)

// instrumentationName scopes the promote tracer to the same instrumentation
// library as the rest of the agent, so all agent spans share one scope.
const instrumentationName = "github.com/joustmania/agent"
