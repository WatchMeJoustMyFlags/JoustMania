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
// fitness.*, agent.objectives and interventions.allowed attributes have no
// semantic convention and are custom to this project.
const (
	SignalReceived = "agent.signal_received"
	SpanDecision   = "agent.decision"
	SpanAction     = "agent.action"
)

// SpanDisabled is the kill-switch trace emitted when the existence layer reports
// the agent off (enabled=false). It is a single agent.decision span (no action
// child — nothing was decided) carrying the existence + permission attribution
// lifted from the disabled-path LayerState, so a Jaeger trace shows "agent off"
// with the flags that were in effect (#729). It is rate-limited to one per
// throttleInterval so a disabled agent under heavy signal load does not flood
// the trace backend.
const SpanDisabled = "agent.disabled"

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

// AttrLLMInferError records why a backend-called agent.llm.retro produced no
// conclusion: the low-cardinality error type of the Infer failure, or
// invalid_output for an unparseable reply (#1242: never the error text). Present
// only on the failure path.
const AttrLLMInferError = "llm.infer.error"

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
	// or transport/timeout error) so retro inference latency is always queryable.
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
	// decision / agent.llm.retro spans so a trace can be
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
	// AttrMode is "retro" on the agent.llm.retro span. #1234 removed it (and the
	// mode flag) from the in-game decision spans; decide.backend says who decided.
	AttrMode = "agent.mode"
	// AttrObjectives is the objective weights being pursued (#725/#731).
	AttrObjectives = "agent.objectives"
	// AttrPolicyBatteryThreshold / AttrPolicyMaxPerMinute lift the two numeric
	// permission-layer policy flags onto the decision span so a single trace
	// shows the policy in effect (#729).
	AttrPolicyBatteryThreshold = "policy.battery_threshold"
	AttrPolicyMaxPerMinute     = "policy.max_interventions_per_minute"
	// AttrInterventionsAllowed summarizes the permission list in effect. The
	// individual flag evaluation is additionally recorded as a feature_flag.*
	// span event (semconv) on the decision span.
	AttrInterventionsAllowed = "interventions.allowed"
	// AttrInferenceConfigured / AttrInferenceUsed / AttrInferenceFallback
	// describe the retro's text backend on the agent.llm.retro span: the model,
	// the backend that served ("none" when capture-only) and why not. The model
	// call itself is the backend's gen_ai chat child span.
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
	// AttrDecisionApplied / AttrDecisionNotAppliedReason (#2093) say whether a
	// decision that passed every permission gate was actually written. Absent on a
	// blocked decision (decision.blocked already says so) and on a failed write
	// (the span carries the error). decision.applied=false carries the reason
	// NotAppliedWriteGateOff when the interventions_enabled actuation gate (UC-040
	// A11) turned the write into a no-op.
	AttrDecisionApplied          = "decision.applied"
	AttrDecisionNotAppliedReason = "decision.not_applied_reason"
	// NotAppliedWriteGateOff is the decision.not_applied_reason of a write the
	// interventions_enabled gate dropped.
	NotAppliedWriteGateOff = "write_gate_off"
	// NotAppliedWriteFailed is the reason of a write that returned an error (A27).
	NotAppliedWriteFailed = "write_failed"
	// AttrInterventionsEnabled is the live interventions_enabled actuation gate on
	// agent.flag_state (#2093).
	AttrInterventionsEnabled = "interventions.enabled"
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

// SpanInterventionEffect is the intervention-effect feedback span (#918): emitted
// after a follow-up window (default DefaultEffectWindow) once per APPLIED (dispatched)
// intervention, carrying the BEFORE/AFTER fitness-objective signals for that game and
// their delta, attributed to the intervention id. It closes the game-path MEASUREMENT
// loop — the infra path already has fitness->rollback, the game path had nothing
// (#918). It is MEASUREMENT ONLY: it never reverts the intervention.
//
// One span per evaluated fitness signal (or one aborted span), each a DIRECT remote
// child of the causing agent.decision, backdated to dispatch time so a Jaeger trace
// shows the full "dispatched at T0, measured at T0+window, delta=..." story. No effect
// span parents another (#1325, UC-040 BR-013). The metric
// agent_intervention_effect_delta (see metrics.go) carries the same delta for
// dashboards, for the progress-type signals only (effectMetricSignal, #1444); the M5 dashboard (#791/#792) and the #844 retro consume both.
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

// Targeted-player attributes on agent.decision and agent.action (#1295). Present
// only when the decision targets one player (Decision.TargetSerial non-empty),
// blocked or applied. AttrPlayerSerial is EXACTLY the key the game coordinator
// puts on its player spans, so one Jaeger search (player.serial=MOCK0004) finds
// the game's player span and every agent decision aimed at that player.
// AttrPlayerName is the player's display name as the controller manager reports
// it on controller_info{serial,name} (the same name the coordinator's
// "player: {name}"
// span carries); absent when the agent has not seen a name for the serial.
const (
	AttrPlayerSerial = "player.serial"
	AttrPlayerName   = "player.name"
)

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
	// AttrExperimentThesis is the experiment's explicit human-readable THESIS (#1184):
	// the immutable Intent.Hypothesis — what idea is being tested + its expected
	// effect. Stamped on the agent.experiment ROOT span (#1188/#1190) so a human reading
	// the trace sees the experiment's WHY, not just the flag/value mechanics. It is the
	// same string the durable journal persists and the dossier surfaces as `thesis`.
	AttrExperimentThesis = "experiment.thesis"
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
