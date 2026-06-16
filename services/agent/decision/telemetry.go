package decision

// Span names for the agent's decision audit trail (issue #724). The hierarchy
// is always parent -> child:
//
//	agent.signal_received        one per triggering OTLP Export (metrics OR traces)
//	  └─ agent.decision          one per Decision the rules engine returns
//	       └─ agent.action       one per decision, wrapping the ActionSink call
//
// The root span is signal-AGNOSTIC (#1053): a metric-triggered cycle and a
// trace-triggered cycle both emit agent.signal_received, distinguished by the
// otlp.signal (metrics|traces) + rpc.service attributes — NOT by the span name.
// Metrics are the agent's PRIMARY (~100ms-1s) trigger; the old "span_received"
// name implied trace-only activity and misled readers into thinking metrics
// were not wired (they are — see receiver.go ApplyMetrics -> OnEvaluate).
//
// Traces are only emitted when the rules engine returns at least one decision
// (including decisions that end up blocked) — the trace IS the audit log of
// agent activity, not of agent idle time. Idle cycles (signal arrives, no rule
// fires) are instead made observable via the agent_evaluations_total counter
// (#1053), so "are signals arriving?" is answerable without a span per cycle.
//
// Where OpenTelemetry semantic conventions exist they are used (semconv
// v1.34.0): rpc.* on agent.signal_received (it wraps an inbound OTLP gRPC
// Export), gen_ai.agent.name as the agent identity on agent.decision (the full
// GenAI agent conventions apply once an LLM inference path exists — its
// requireds, e.g. gen_ai.provider.name, are only honest in llm mode), a
// feature_flag.* span event for the interventions.allowed evaluation (the same
// shape the openfeature-hooks-opentelemetry package emits in the Python
// services), and error.type + span status on action failures. The decision.*,
// fitness.*, agent.mode, agent.objectives and interventions.allowed attributes
// have no semantic convention and are custom to this project.
const (
	SignalReceived = "agent.signal_received"
	SpanDecision   = "agent.decision"
	SpanAction     = "agent.action"
)

// SpanDisabled is the kill-switch trace emitted when the existence layer reports
// the agent off (enabled=false). It is a single agent.decision span (no action
// child — nothing was decided) carrying the existence + capability attribution
// lifted from the disabled-path LayerState, so a Jaeger trace shows "agent off"
// with the flags that were in effect (#729). It is rate-limited to one per
// throttleInterval so a disabled agent under heavy signal load does not flood
// the trace backend.
const SpanDisabled = "agent.disabled"

// SpanLLMPrompt is the M4 prompt-capture span (#739): on every llm-mode cycle
// the loop builds the prompt it WOULD send to a backend and records it on this
// dedicated span — even on cycles where the rules engine returns zero decisions,
// because the agent.decision audit spans are lazy (no span on an idle cycle). A
// dedicated span makes "what would the agent have asked the LLM" greppable in
// Jaeger by name, independent of whether a decision was produced. Throttled to
// one per throttleInterval (shared with the evaluate log / agent.disabled span).
const SpanLLMPrompt = "agent.llm.prompt"

// SpanLLMInfer is the actual-inference span (#739): emitted when the llm path
// calls a resolved backend's Infer (NOT the capture path, which only renders the
// prompt). It is a child of agent.signal_received and a SIBLING of the resulting
// agent.decision span, carrying the GenAI request attribution and — on a parsed
// response — the model's reasoning + chosen objective + action. On an Infer error
// or an unparseable response it carries AttrLLMInferError and the cycle falls back
// to rules; the gen_ai.* shape lets Jaeger treat it as a model call.
const SpanLLMInfer = "agent.llm.infer"

// SpanLLMApply is the ASYNC-application audit root (#917): emitted when an
// async inference RESULT lands, seconds after the cycle that fired it, carrying
// the whole re-validation outcome. Because the firing cycle's agent.signal_received
// has long since ended (the loop never blocks on Infer), the async result cannot
// hang off it — so the apply path opens its OWN root span backdated to the moment
// inference completed. It parents the agent.llm.infer call span and, when the
// result is APPLIED, the resulting agent.decision -> agent.action children, so a
// Jaeger trace shows the full "fired at T0, answered at T0+latency, applied/
// discarded" story. It always carries inference.latency_ms and, on a drop, the
// inference.discarded_reason; an applied result carries the normal decision schema.
const SpanLLMApply = "agent.llm.apply"

// SpanLLMInferCall is the OUTBOUND-HTTP client span wrapped around the async
// backend.Infer call (#1096 async follow-up). On the SYNC path agent.llm.infer is
// active in ctx when Infer runs, so #1112's traceparent injection (openai.go) parents
// the litellm gateway's gen_ai span under it. On the ASYNC path (#917) the attribution
// agent.llm.infer span is emitted LATER, at apply time (emitAsyncInferSpan), so when
// the HTTP call actually fires in runInfer there is NO infer span in ctx and the
// gateway span would orphan onto the async-apply root (or a parent-less trace).
//
// Rather than move the deliberately apply-time attribution span (it is backdated and
// timed for fire->apply accounting), we open this MINIMAL client span JUST around the
// outbound call so its ctx carries an active, sampled span for the injector to read.
// It is a short-lived SpanKindClient marker carrying only the request model — no
// gen_ai attribution duplication; the apply-time agent.llm.infer keeps owning that.
// It shares the call's trace, so the gateway span nests under the same trace as the
// agent decision, restoring "one trace" on the production async path too.
const SpanLLMInferCall = "agent.llm.infer.call"

// AttrLLMLatencyMs is the wall time a single async inference took, fire to
// completion, in integer milliseconds (#917). Recorded on the agent.llm.apply
// root and the agent.llm.infer span for EVERY async result — applied, discarded,
// or timed-out — so inference latency is always queryable in Jaeger (acceptance:
// "audit span records inference latency").
const AttrLLMLatencyMs = "inference.latency_ms"

// AttrLLMDiscardedReason records WHY an async inference result was dropped at
// apply time instead of being dispatched (#917). Present ONLY on a discarded
// result; its absence means the result was applied (or fell back to rules and the
// rules decision was applied). The value is one of the DiscardReason* constants.
const AttrLLMDiscardedReason = "inference.discarded_reason"

// Async-inference discard reasons (#917). When an async result lands it is
// re-validated against the CURRENT game context (fetched fresh at apply time, NOT
// the fire-time snapshot — the 2-10s in flight may have moved the game on). A
// result that fails any check is dropped and the reason rides agent.llm.apply as
// inference.discarded_reason. The deterministic rules fallback then decides for the
// current context where appropriate (timeout), so the system always decides.
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
	// DiscardPermissionRevoked: interventions.allowed no longer permits the chosen
	// action at apply time (the allow-list was tightened, or the battery gate now
	// blocks the target). The SAME permission chain a sync decision runs is re-run
	// at apply time; an allow-list/battery block surfaces as this reason.
	DiscardPermissionRevoked = "permission_revoked"
	// DiscardRateLimited: the per-game weighted rate limiter no longer affords the
	// action at apply time (the budget filled with other interventions while the
	// call was in flight). Distinct from permission_revoked so a budget drop is
	// attributable separately from an allow-list/battery change.
	DiscardRateLimited = "rate_limited"
	// DiscardTimeout: the inference exceeded the latency budget (context deadline)
	// and was dropped outright. The deterministic rules engine then decides for the
	// CURRENT context so the game still gets a decision (#741's timeout -> rules
	// chain), recorded honestly on the same agent.llm.apply trace.
	DiscardTimeout = "timeout"
)

// AttrLLMContextGames is the M7-2 rolling-context-window size recorded on the
// inference span (#929): the COUNT of recent game summaries actually injected into
// this call's prompt as the cross-game narrative context block. It is the live N
// (flags.Snapshot.ContextGames, clamped to [0, gamewindow.RetentionCap] and bounded
// by how many games have ended), NOT the configured flag value — so the span shows
// what the model really saw. Recorded on agent.llm.infer (the REAL inference span,
// SpanLLMInfer — the issue's "agent.inference") and, cheaply, on agent.llm.prompt
// (the capture span) so the count is queryable on both. Naming note: the issue
// prose says agent.inference, but the codebase's real inference span is
// agent.llm.infer (#739); the attribute lands there, not on an invented span.
const AttrLLMContextGames = "agent.llm.context_games"

// AttrLLMContextNotePresent / AttrLLMContextNoteLen are the M7-3 operator-note view
// recorded on EVERY llm call (#930): whether a validated operator context note was
// injected into this call's prompt, and its rune length. We deliberately record a
// BOUNDED, LOW-CARDINALITY view — a bool + an int — rather than the note text itself:
// the note is operator-set (not user PII), but it is free-text and can be long/varied,
// so stamping the raw value on every span would balloon span cardinality and storage.
// present+len is enough to answer the operational questions ("is a note live?", "how
// big?") and to satisfy the acceptance criterion that the injected context is visible
// as a span attribute on every LLM call. Both are recorded on agent.llm.infer (the REAL
// inference span, SpanLLMInfer — the issue's "every LLM call") and, cheaply, on
// agent.llm.prompt (the capture span). present=false / len=0 means no note was injected
// (unset, flagd-unreachable, or rejected by ValidateContextNote as empty/oversized/
// control-char) — indistinguishable on the span from "no note", which is correct: a
// rejected note leaves the prompt exactly as if none were set.
const (
	AttrLLMContextNotePresent = "agent.llm.context_note_present"
	AttrLLMContextNoteLen     = "agent.llm.context_note_len"
)

// AttrLLMInferError records why an llm.infer span did not yield a usable Decision
// (#739): the Infer transport error, or the llm.Decode rejection reason
// (empty / not-JSON / missing-field / out-of-vocab-objective). Present only on the
// failure path; its presence on the span is the marker that the cycle fell back to
// rules with FallbackUnparseable.
const AttrLLMInferError = "llm.infer.error"

// Custom attribute keys of the agent.llm.prompt span (#739). The gen_ai.*
// attributes use semconv constants where they exist; these three carry the
// captured prompt text and size, which have no semantic convention.
const (
	// AttrLLMPromptSystemSHA is the FINGERPRINT of the System prompt (#1168): the
	// short hex SHA-256 (systemPromptSHA) of the full system text, stamped INSTEAD of
	// the text itself. The system prompt is large and effectively constant (a pure
	// function of the low-cardinality prompt_variant + mode + interventions_allowed,
	// all already on this span — plus the #929 PRIOR-GAMES block / #930 note when
	// present), so shipping its full text on every span (4 shadow games/tick) was the
	// redundant bulk that drove the #1167 traces-pipeline collector OOM. The full text
	// is emitted ONCE per distinct fingerprint on the agent.llm.system_prompt
	// reference LOG line (prompt_fingerprint.go), so this hash is always resolvable to
	// the exact text without paying per span.
	AttrLLMPromptSystemSHA = "llm.prompt.system_sha256"
	// AttrLLMPromptUser carries the User prompt text the agent would have sent this
	// cycle — the VARIABLE, valuable reasoning input (per-player arcs, timeline), so
	// it is KEPT on the span (capped at maxUserPromptBytes with a truncation marker so
	// a pathological game cannot reintroduce per-span bloat). The Go SDK applies no
	// attribute length limit and the collector does not truncate.
	AttrLLMPromptUser = "llm.prompt.user"
	// AttrLLMPromptBytes is len(system)+len(user): the FULL prompt size in bytes (int),
	// unchanged by #1168 (it still counts the system prompt even though the system TEXT
	// is no longer emitted), so the at-a-glance sizing is the same number as before.
	AttrLLMPromptBytes = "llm.prompt.bytes"
)

// Custom attribute keys for the retro INFERENCE CONCLUSION (#1179): the values
// captured on the agent.llm.retro span once the offline analyst actually answers.
// Until #1179 the retro span only carried the PROMPT (a 10µs capture-only stub);
// these keys carry the analyst's decoded reply so the maintainer can read the agent's
// post-game LEARNING in Jaeger. They live in the llm.retro.* namespace alongside the
// prompt keys (AttrLLMRetroSystemSHA etc., retro_capture.go) so the two never collide.
//
// The per-suggestion detail is NOT crammed into a single high-cardinality attribute:
// each suggestion is emitted as one span EVENT (retro.suggestion) carrying the
// intervention_type / emphasis / reason, so a reader can expand the timeline and the
// suggestions stay queryable without ballooning the attribute set.
const (
	// AttrLLMRetroSessionAssessment is the analyst's one-sentence read on the game
	// (RetroResponse.SessionAssessment). Stamped only on a parsed reply.
	AttrLLMRetroSessionAssessment = "llm.retro.session_assessment"
	// AttrLLMRetroSessionFocus is the goal the analyst says the next session should
	// lean toward (RetroResponse.SessionFocus). Stamped only on a parsed reply.
	AttrLLMRetroSessionFocus = "llm.retro.session_focus"
	// AttrLLMRetroSuggestionCount is the number of tuning suggestions the analyst
	// returned (len(RetroResponse.Suggestions)) — 0 is a valid, healthy-session reply.
	AttrLLMRetroSuggestionCount = "llm.retro.suggestion_count"
	// AttrLLMRetroLatencyMs is the wall time the inference call took, in integer
	// milliseconds. Recorded on EVERY backend-called retro (success, decode failure,
	// or transport/timeout error) so retro inference latency is always queryable —
	// the retro-namespace counterpart of AttrLLMLatencyMs.
	AttrLLMRetroLatencyMs = "llm.retro.latency_ms"
	// AttrLLMRetroParseOK is true when DecodeRetro turned the reply into a
	// RetroResponse, false when the reply was unparseable. Always present on a
	// backend-called retro; absent on the capture-only fallback (no backend).
	AttrLLMRetroParseOK = "llm.retro.parse_ok"
	// AttrLLMRetroResponseSHA is the FINGERPRINT (short hex SHA-256) of the analyst's
	// RAW reply text (#1169 pattern): stamped on the span INSTEAD of the full text,
	// which is large and would re-load the #1167 traces pipeline. The full raw reply is
	// emitted ONCE per distinct fingerprint on the agent.llm.retro_response reference
	// LOG line, so this hash is always resolvable to the exact text.
	AttrLLMRetroResponseSHA = "llm.retro.response_sha256"
)

// SpanLLMRetroSuggestion is the span EVENT name carrying one decoded retro suggestion
// (#1179): one event per RetroResponse.Suggestion on the agent.llm.retro span,
// attributes intervention_type / emphasis / reason. Emitting them as events (not as
// one fat attribute) keeps each suggestion queryable in Jaeger while bounding the
// attribute set.
const SpanLLMRetroSuggestion = "retro.suggestion"

// Span-event attribute keys for a single retro.suggestion event (#1179): the three
// fields of a RetroSuggestion. Low-cardinality type/emphasis labels + the free-text
// reason, scoped to the event so they never collide with span-level llm.retro.* keys.
const (
	AttrRetroSuggestionType     = "intervention_type"
	AttrRetroSuggestionEmphasis = "emphasis"
	AttrRetroSuggestionReason   = "reason"
)

// SpanLLMRetroResponseRef is the message of the once-per-fingerprint reference LOG
// line for the analyst's RAW reply (#1169 pattern, #1179): the full raw text is large
// and recorded-only, so — exactly like the system-prompt reference (prompt_fingerprint.go)
// — it rides a LOG line (not the traces pipeline) emitted ONCE per distinct
// response_sha256, keeping the big text off the span while staying resolvable.
const SpanLLMRetroResponseRef = "agent.llm.retro_response"

// SpanLLMRetroFailed is the WARN log message emitted when a backend-called retro
// could not yield a conclusion (#1179): a transport/timeout error or an unparseable
// reply. It is the silent-failure guard — a retro that asked the model but got nothing
// usable is logged loudly (with the cause) instead of vanishing, so the maintainer
// sees that the post-game learning attempt failed rather than mistaking "no
// conclusion" for "nothing to say".
const SpanLLMRetroFailed = "agent.llm.retro_failed"

// Custom attribute keys of the decision-span schema (issue #724). Attributes
// covered by a semantic convention (gen_ai.agent.name, rpc.*, error.type,
// feature_flag.*) use the semconv constants directly and are not listed here.
const (
	// AttrGameKind is the kind of game the decision was made for: "real",
	// "shadow", or "" (unknown). Lifted from GameContext.GameKind (#845) onto the
	// decision / agent.llm.prompt / agent.llm.retro spans so a trace can be
	// filtered by game kind — schema-complete (empty string when unknown).
	AttrGameKind = "game.kind"
	// AttrSessionID and AttrGameID both carry GameContext.SessionID — which, since
	// PR A's early game_id adoption, IS the real game_id. session.id is the original
	// audit key; game.id is an explicit alias (#845 PR C) so Jaeger queries by
	// game.id are symmetrical with the coordinator's own game.id-tagged spans, and
	// two concurrent games' decision traces are independently attributable.
	AttrSessionID = "session.id"
	AttrGameID    = "game.id"
	// AttrGameTraceID is the hex trace_id of the originating game-coordinator trace
	// (#1133, Phase 2 of #1088). Stamped in TWO places when a valid game trace_id is
	// ingested: (1) as an attribute on the OTel Link the agent.decision span adds to
	// that game trace (gameTraceLink), and (2) as a plain attribute on the decision
	// span itself — so it is searchable as a span tag in Jaeger even on backends that
	// don't surface Link attributes in search. Empty/absent when no valid game
	// trace_id was ingested (graceful fallback: no link and no attribute are added).
	AttrGameTraceID = "game.trace_id"
	// AttrGameTraceSpanID is the hex span_id of the originating game-coordinator
	// root game span (#1157, follow-up to #1133). Stamped in the SAME two places as
	// AttrGameTraceID (the gameTraceLink Link attribute + a plain span attribute) so
	// the Link references the actual game-start span and the span_id is searchable
	// as a span tag. Empty/absent when no valid game span_id was ingested.
	AttrGameTraceSpanID = "game.trace_span_id"
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
	// fallback_reason = FallbackNoBackend when mode=llm fell back.
	DefaultInference = "none"
	// InferenceRules is inference.used on every cycle until the M4 LLM path
	// runs: the deterministic rules engine is what actually decided.
	InferenceRules = "rules"
	// FallbackNoBackend is inference.fallback_reason when the #847 gate ADMITTED an
	// llm attempt but resolve_backend (#741) found the WHOLE chain unreachable, so it
	// bottoms out at the always-available rules engine (inference.used="rules"). It is
	// the honest "the llm path was wanted but nothing answered" reason — distinct from
	// FallbackEndpointUnreachable, where a LOWER llm tier still serves. Empty when not
	// applicable (mode=rules, or a configured-tier-reachable llm cycle). Until #739 a
	// reachable tier does not actually decide either, but it carries no fallback_reason
	// (configured == used); only a fully-unreachable chain reports this.
	FallbackNoBackend = "no_backend_available"
	// LLM call-gate fallback reasons (#847). When the three-layer gate denies an
	// llm cycle, the cycle falls back to the rules engine WITHOUT building or
	// capturing the prompt, and the decision span's inference.fallback_reason
	// carries the specific gate reason instead of FallbackNoBackend — every
	// skipped LLM call is attributable in Jaeger. They are evaluated in order:
	// eligibility, then cadence, then budget.
	//
	// FallbackNotEligible: the game's kind is not in llm.eligible_game_kinds (e.g.
	// a shadow game under the default ["real"] list) — the cycle never even
	// considers the cadence/budget layers.
	FallbackNotEligible = "llm_not_eligible"
	// FallbackInterval: the per-game cadence floor (llm.min_decision_interval_seconds)
	// has not elapsed since this game's last admitted llm attempt.
	FallbackInterval = "llm_interval"
	// FallbackBudgetExhausted: the global per-minute request budget
	// (llm.max_requests_per_minute, shared across all games) is full this window.
	FallbackBudgetExhausted = "llm_budget_exhausted"
	// FallbackEndpointUnreachable is the inference.fallback_reason when the #847 gate
	// ADMITTED an llm attempt but resolve_backend (#741) had to degrade PAST the
	// configured tier because a higher tier's endpoint was unreachable — a LOWER llm
	// tier serves instead (e.g. configured=claude but the cloud endpoint is down, so
	// gemma3:4b or phi4-mini resolves). It is distinct from FallbackNoBackend: this
	// reason means a real llm tier WILL serve (#739), just not the configured one;
	// FallbackNoBackend means the WHOLE chain was unreachable and rules decided.
	// Recorded together with inference.used=<the lower tier> on the decision span.
	FallbackEndpointUnreachable = "endpoint_unreachable"
	// FallbackUnparseable is the inference.fallback_reason when the #847 gate ADMITTED
	// an llm attempt AND resolve_backend (#741) resolved a REACHABLE tier whose Infer
	// was actually called (#739), but the model's response could NOT be turned into a
	// valid Decision — the call errored, the body was not the contracted JSON, a
	// required field was missing, or the chosen intervention was out-of-vocabulary
	// (not in the agent's known intervention set). The cycle falls back to the rules
	// engine, and inference.used is recorded as the TIER THAT WAS CALLED (not "rules")
	// so the span honestly shows "this tier answered, but unusably". This is the
	// safety floor of the LLM path: an unparseable/invalid/out-of-vocab response NEVER
	// dispatches an arbitrary action — it always degrades to the deterministic engine
	// with this reason. Distinct from FallbackNoBackend (no tier was reachable at all,
	// Infer was never called).
	FallbackUnparseable = "llm_unparseable"
	// FallbackInflight is the inference.fallback_reason when the #847 gate ADMITTED an
	// llm cycle and a tier was reachable, but the per-game pile-up guard found an Infer
	// ALREADY in flight for this game (#917): the previous cycle's async call outran
	// the cadence interval. The loop does NOT launch a second concurrent Infer — it
	// falls back to the rules engine for this cycle so the game still gets a decision,
	// and records this reason so a skipped fire is attributable in Jaeger. The earlier
	// in-flight call still applies its own result on its own agent.llm.apply trace.
	FallbackInflight = "llm_inflight"
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

// SpanInterventionEffect is the intervention-effect feedback span (#918): emitted
// after a follow-up window (default DefaultEffectWindow) once per APPLIED (dispatched)
// intervention, carrying the BEFORE/AFTER fitness-objective signals for that game and
// their delta, attributed to the intervention id. It closes the game-path MEASUREMENT
// loop — the infra path already has fitness->rollback, the game path had nothing
// (#918). It is MEASUREMENT ONLY: it never reverts the intervention.
//
// Because the dispatch cycle's agent.signal_received root has long since ended (the
// follow-up window is seconds later), the effect cannot hang off it — so the sampler
// opens its OWN root span backdated to dispatch time, parenting an
// agent.intervention.effect span per evaluated objective, so a Jaeger trace shows the
// full "dispatched at T0, measured at T0+window, delta=..." story. The metric
// agent_intervention_effect_delta (see metrics.go) carries the same delta for
// dashboards/alerts; the M5 dashboard (#791/#792) and the #844 retro consume both.
const SpanInterventionEffect = "agent.intervention.effect"

// Custom attribute keys of the intervention-effect span (#918). The fitness signal
// key (e.g. "endurance.session_progress") is the SAME dotted vocabulary the decision
// span's fitness.evaluated carries, so baseline and follow-up sample identical
// quantities. baseline/follow_up/delta are the three numeric stamps; aborted marks a
// follow-up that could not complete (game ended/evicted before the window elapsed).
const (
	// AttrEffectInterventionID is the dispatch-unique id tying an effect record to the
	// intervention that produced it. The decision span carries the same id as
	// decision.intervention_id, so a Jaeger query joins "what the agent did" to "what
	// happened next".
	AttrEffectInterventionID = "intervention.id"
	// AttrEffectIntervention is the intervention name (decision.action), e.g.
	// "grant_shield" — a low-cardinality label for grouping effects by type.
	AttrEffectIntervention = "intervention.type"
	// AttrEffectObjective is the objective whose fitness this record measures
	// (endurance/balanced/accelerate) — the objective the intervention served.
	AttrEffectObjective = "intervention.objective"
	// AttrEffectSignal is the dotted fitness signal key whose delta this span carries
	// (e.g. "endurance.session_progress"), from the shared fitness.evaluated vocabulary.
	AttrEffectSignal = "intervention.signal"
	// AttrEffectBaseline / AttrEffectFollowUp / AttrEffectDelta are the three numeric
	// stamps: the signal value at dispatch, at follow-up, and follow_up-baseline.
	AttrEffectBaseline = "intervention.fitness_baseline"
	AttrEffectFollowUp = "intervention.fitness_followup"
	AttrEffectDelta    = "intervention.fitness_delta"
	// AttrEffectWindowSeconds is the follow-up window length in seconds, so a consumer
	// knows over what interval the delta was measured.
	AttrEffectWindowSeconds = "intervention.window_seconds"
	// AttrEffectAborted is true when the follow-up could NOT be measured because the
	// game ended or was evicted before the window elapsed (#918 cancellation path): the
	// record is emitted clearly marked, follow_up/delta are absent, and no metric is
	// recorded for that objective.
	AttrEffectAborted = "intervention.effect_aborted"
)

// AttrDecisionInterventionID stamps the dispatch-unique effect id on the decision
// span too (#918), so the agent.decision span that dispatched an intervention and the
// agent.intervention.effect span that measured it carry the SAME id and can be joined
// in Jaeger. Present only on dispatched (applied) decisions that schedule a follow-up.
const AttrDecisionInterventionID = "decision.intervention_id"

// Experiment / cohort lifecycle spans (#1140 Slice C). Before this the entire
// "learn from games" loop (experiment_loop.go) was LOG-ONLY — the retro/experiment
// phase was invisible in traces. These spans make that loop an actual trace, in the
// established dot-notation namespace, each carrying experiment.id / arm / fitness
// attributes and (where a game trace_id is in scope) a span Link to the originating
// game trace (the same #1133 link-from-hex primitive gameTraceLink uses). They are
// OBSERVABILITY ONLY: they wrap the existing fitness/verdict/conclusion computations
// without changing verdict math, fitness computation, promotion gating, or the loop's
// control flow.
const (
	// SpanExperiment is the long-lived experiment ROOT span (#1188, epic #1181 PR2):
	// one span per experiment, started when the experiment becomes RUNNING and ended on
	// its terminal transition (concluded/discarded/promoted/aborted/done). It is the
	// trace root the per-experiment ANALYSIS spans (experiment.fitness/evaluate/verdict,
	// agent.llm.retro, agent.game.summary, code_improvement.promote) re-parent under for
	// an experiment-bound game, so the whole hypothesis-test narrative is ONE trace.
	// Carries experiment.id / flag_key / objective / target_n / arms. It outlives the
	// game spans it analyzes (the analysis runs "in between" games), which is why the
	// analysis spans can be its CHILDREN. Observability only.
	SpanExperiment = "agent.experiment"
	// SpanExperimentFitness wraps the per-shadow-game fitness scalar computation in
	// onGameEnd: the gameFitnessFunc that scores one finished experiment game into the
	// [0,1] sample the cohort aggregator folds. Carries experiment.id / arm / game.id /
	// objective + the resulting experiment.fitness, Linked to the game trace.
	SpanExperimentFitness = "experiment.fitness"
	// SpanExperimentEvaluate wraps one experiment-conclusion: folding a concluded
	// shadow game into its arm (ConcludeGame) and the resulting lifecycle status.
	// Carries experiment.id / game.id / the resulting experiment.status, Linked to the
	// game trace. It is the parent of any experiment.verdict span.
	SpanExperimentEvaluate = "experiment.evaluate"
	// SpanExperimentVerdict is the paired-difference decision span: emitted under
	// experiment.evaluate when the conclusion produced a rolling verdict (the #979
	// min-N + effect-size comparison the registry computed). Carries the verdict
	// outcome / delta / significance. It records the verdict, it does NOT compute it
	// (the registry owns the math) — observability only.
	SpanExperimentVerdict = "experiment.verdict"
)

// Custom attribute keys for the experiment-lifecycle spans (#1140 Slice C). They
// reuse the established experiment.* vocabulary (AttrExperimentID / AttrExperimentArm /
// AttrExperimentFitness, defined in retro_capture.go) and add the conclusion-status
// and verdict keys. No cross-session player identity is ever stamped — only
// experiment.id / arm / game.id (the no-player-identity constraint, #23).
const (
	// AttrExperimentStatus is the experiment lifecycle status that resulted from
	// folding a game (e.g. "running" / "concluded"), recorded on experiment.evaluate.
	AttrExperimentStatus = "experiment.status"
	// AttrExperimentObjective is the experiment's fitness objective
	// (endurance/balanced/accelerate) the per-game fitness was scored against,
	// recorded on experiment.fitness. It is also stamped on the agent.experiment ROOT
	// span (#1188).
	AttrExperimentObjective = "experiment.objective"
	// AttrExperimentFlagKey / AttrExperimentTargetN / AttrExperimentArms are the
	// agent.experiment ROOT span's identifying attributes (#1188): the flag/candidate
	// under test, the per-arm target sample size, and the comma-joined arm names. They
	// describe the experiment as a whole (not a single game), so they live only on the
	// root span.
	AttrExperimentFlagKey = "experiment.flag_key"
	AttrExperimentTargetN = "experiment.target_n"
	AttrExperimentArms    = "experiment.arms"
	// AttrVerdictOutcome / AttrVerdictDelta / AttrVerdictSignificant carry the rolling
	// paired-difference verdict the registry computed: the outcome label
	// ("inconclusive"/"promote"/"discard"), the experimental-minus-control delta, and
	// whether it cleared the significance bar. Recorded on experiment.verdict.
	AttrVerdictOutcome     = "verdict.outcome"
	AttrVerdictDelta       = "verdict.delta"
	AttrVerdictSignificant = "verdict.significant"
)

// instrumentationName scopes the agent's tracer.
const instrumentationName = "github.com/joustmania/agent"
