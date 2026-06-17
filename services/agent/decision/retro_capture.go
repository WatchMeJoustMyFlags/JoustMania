package decision

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	otelmetric "go.opentelemetry.io/otel/metric"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/llm"
)

// retro_capture.go is the POST-GAME counterpart of llm_capture.go (#844). When a
// game ends (the gamecontext.Store fires its OnGameEnd hook on the GameActive
// true->false transition), the RetroCoordinator builds the retrospective prompt
// the agent WOULD send to an offline analyst and records it on a dedicated
// `agent.llm.retro` span, exactly once per session. Like the #739 in-game
// capture, no backend is called yet — this is capture-first.
//
// STRUCTURAL GUARANTEE: the coordinator never touches gate.ShouldEvaluate, the
// decision Loop, or the per-game intervention rate limiter. A retrospective
// therefore cannot consume the in-game INTERVENTION budget (decision/ratelimit.go)
// — there is simply no code path from here to it.
//
// LLM BUDGET (#847): a retrospective IS one LLM request, so it MUST draw from the
// same GLOBAL llm.max_requests_per_minute budget the in-game gate uses (#844 +
// #847 acceptance #7). The coordinator therefore takes the one shared *llmBudget
// (injected via SetLLMBudget from main.go, the same instance every per-game Loop
// holds). When the budget is exhausted at game end the retro is GATED: it records
// agent_llm_gated_total{reason=llm_budget_exhausted} and captures nothing — a
// retrospective cannot blow the global LLM cap. NOTE the retro is intentionally
// NOT subject to the eligibility or cadence layers: it is a once-per-session,
// game-kind-agnostic offline analysis, so only the global budget bounds it.

// SpanLLMRetro is the post-game retrospective capture span (#844): emitted once
// per session at game end, carrying the prompt the agent would send to an offline
// LLM analyst. Named distinctly from agent.llm.prompt so retro captures are
// trivially greppable in Jaeger by name.
const SpanLLMRetro = "agent.llm.retro"

// Custom attribute keys of the agent.llm.retro span (#844). They mirror the
// agent.llm.prompt keys (telemetry.go) but in the retro namespace so the two
// capture kinds never collide in a single trace view.
const (
	// AttrLLMRetroSystemSHA is the FINGERPRINT of the retrospective System prompt
	// (#1168), the retro-namespace counterpart of AttrLLMPromptSystemSHA: the short
	// hex SHA-256 of the full system text, stamped INSTEAD of the text. The retro
	// system prompt is large and effectively constant (it got the physics model in
	// #1160), so shipping its full text per retro span was part of the #1167
	// traces-pipeline collector OOM. The full text is emitted ONCE per distinct
	// fingerprint on the shared agent.llm.system_prompt reference LOG line
	// (prompt_fingerprint.go), so this hash is always resolvable to the exact text.
	AttrLLMRetroSystemSHA = "llm.retro.system_sha256"
	// AttrLLMRetroUser carries the User retrospective prompt text — the VARIABLE,
	// valuable analysis input — KEPT on the span, capped at maxUserPromptBytes with a
	// truncation marker. The Go SDK applies no attribute length limit and the
	// collector does not truncate.
	AttrLLMRetroUser = "llm.retro.user"
	// AttrLLMRetroBytes is len(system)+len(user): the FULL prompt size in bytes (int),
	// unchanged by #1168 (system TEXT no longer emitted, but still counted).
	AttrLLMRetroBytes = "llm.retro.bytes"
)

// Structured game-outcome and experiment-correlation attribute keys (#1100). They
// surface the game outcome (winner / final active players / elimination count /
// duration) and the shadow-experiment attribution (id / arm / recorded fitness)
// as DISCRETE, queryable span attributes — the outcome was previously buried only
// in the llm.retro.user prompt TEXT, and the experiment correlation was absent
// entirely, so a retro could not be joined to the experiment/arm that spawned it
// or pivoted on its outcome. The outcome keys reuse the established game.* /
// experiment.* vocabulary (game.id/game.kind on this span, experiment.id/
// experiment.arm on the gamecontext span attrs, gamesummary's duration_seconds).
// Every key is ADDITIVE and OMITTED when its signal is absent (a non-experiment
// game carries no experiment.* attrs; an unobserved outcome field is dropped), so
// the schema never emits empty or garbage tags.
const (
	// AttrGameWinner is the sole survivor's serial. Omitted when there is no single
	// unambiguous winner (zero or multiple survivors).
	AttrGameWinner = "game.winner"
	// AttrGameFinalActivePlayers is the session's final active-player count. Omitted
	// when never observed.
	AttrGameFinalActivePlayers = "game.final_active_players"
	// AttrGameEliminationCount is the number of eliminations recorded for the game.
	AttrGameEliminationCount = "game.elimination_count"
	// AttrGameDurationSeconds is the final observed game duration. Omitted when unknown.
	AttrGameDurationSeconds = "game.duration_seconds"
	// AttrExperimentID / AttrExperimentArm are the shadow-experiment attribution this
	// game belonged to. They mirror the gamecontext span attribute keys (extract_spans.go)
	// so a Jaeger query joins the retro to the experiment that spawned it. Omitted for a
	// real / standalone game (no experiment id).
	AttrExperimentID  = "experiment.id"
	AttrExperimentArm = "experiment.arm"
	// AttrExperimentFitness is the game's recorded objective fitness (the #965 finalized
	// per-game sample) when it is available at retro time. Omitted when no fitness lookup
	// is wired or the store has no settled sample for this game yet.
	AttrExperimentFitness = "experiment.fitness"
)

// retroModeValue is the agent.mode value recorded on the retro-capture span. The
// span only ever exists at game end on the offline-analyst path, so it is
// constant — distinct from the in-game llmModeValue.
const retroModeValue = "retro"

// retroDedupeWindow bounds the per-session dedupe memory: the SessionIDs of the
// most recently captured retros are retained so a repeated game-end for any of
// them is suppressed. With multiple concurrent games ending (#845 PR B), ends
// interleave (game A ends, game B ends, game A' ends), so a single last-session
// string is insufficient — it would only remember B and re-capture A. An LRU of
// the last few session ids handles arbitrary interleaving; 8 comfortably exceeds
// GAME_MAX_CONCURRENT_GAMES=4 even with one stale id lingering per game.
const retroDedupeWindow = 8

// RetroCoordinator builds and captures the post-game retrospective prompt. It is
// wired to every gamecontext.Store partition's OnGameEnd in main() (#845 PR B), so
// one coordinator dedupes across all partitions. It is safe for concurrent use:
// per-partition game-end transitions are each serialized by their store, but
// concurrent partitions may fire simultaneously, so the dedupe state is
// mutex-guarded.
type RetroCoordinator struct {
	// Tracer produces the agent.llm.retro span; tests inject a recording tracer.
	Tracer trace.Tracer
	// Log records the metadata-only agent.llm.retro_captured line (never prompt text).
	Log *slog.Logger
	// Flags is the same flag-source interface the Loop uses; evaluated with a
	// background context at game end (there is no inbound RPC context then).
	Flags FlagSource
	// now is injectable for tests; nil uses time.Now.
	now func() time.Time

	// budget is the GLOBAL per-minute LLM request budget (#847), shared with every
	// per-game Loop. A retro draws one slot from it; a nil budget means the retro is
	// ungated by the global cap (used by tests / single-purpose wiring that does not
	// share a budget — capture proceeds as before #847). Injected via SetLLMBudget.
	budget *llmBudget
	// resolver is the SHARED inference fallback-chain resolver (#741/#964), injected
	// via SetResolver — the SAME instance every per-game Loop holds (#1080). When a
	// real inference backend is configured (AGENT_INFERENCE_BACKEND=openai), the retro
	// resolves against it (gemma4 @ AGENT_INFERENCE_BASE_URL) so retro attribution
	// targets the configured model/tier and only degrades when it is genuinely
	// unreachable — instead of always hitting the unreachable phi4-mini legacy tier.
	// A nil resolver (tests / stub-default wiring without one) preserves the pre-#1080
	// behavior: model = the agent.model flag, used = "none", fallback =
	// no_backend_available, exactly as before.
	resolver *Resolver
	// llmGated counts gated retro captures (agent_llm_gated_total{reason=...}),
	// sharing the in-game counter name. Defaults to a no-op so tests need not wire it.
	llmGated otelmetric.Int64Counter

	// fitnessLookup resolves a finished game's recorded objective fitness by game_id
	// (#1100), reading the #965 finalized-per-game FitnessStore. When wired and a
	// settled sample exists, the retro stamps experiment.fitness on the span so a
	// retro is joinable to the fitness its arm scored. nil (the default / tests /
	// pre-#1100 wiring) means no fitness attribute is ever emitted — the span is
	// byte-identical to before except for the always-additive outcome attrs. Injected
	// via SetFitnessLookup; not safe to call concurrently with OnGameEnd.
	fitnessLookup func(gameID string) (float64, bool)

	// experimentParent resolves the long-lived agent.experiment ROOT span's SpanContext
	// by experiment_id (#1188), reading the registry's ExperimentSpanContext accessor.
	// When wired and the finished game is experiment-bound (GameContext.ExperimentID set)
	// AND the experiment still has a live root span, the agent.llm.retro span is started
	// as a remote CHILD of the experiment span instead of an own-root span — so the
	// retro joins the experiment's trace. This is sound even though the retro is ASYNC
	// (post-#1179, the infer goroutine ends the span seconds later): the experiment span
	// OUTLIVES its games (the registry ends it only at the terminal transition, well
	// after the per-game retro), so the parent is alive when the retro starts. A nil
	// lookup (the default / tests), a non-experiment game, or a torn-down experiment
	// (ok=false) falls back to the pre-#1188 game-LINKED own-root span — unchanged.
	// Injected via SetExperimentParent; not safe to call concurrently with OnGameEnd.
	experimentParent func(experimentID string) (trace.SpanContext, bool)

	// suggestionSink forwards each DECODED retro suggestion (#1184) to a downstream
	// consumer — in production the experiment loop's retro-seeded THESIS proposer,
	// which turns a suggestion into a candidate experiment whose thesis is seeded from
	// the retro's reasoning, closing the game→retro→thesis→experiment loop. It is
	// PURELY additive: the suggestion is still stamped as a span event regardless. nil
	// (the default / tests / proposer-disabled) means the suggestion is recorded-only,
	// byte-identical to pre-#1184. The sink itself decides whether to act on the
	// suggestion (the proposer is gated default-OFF), so wiring the sink is safe even
	// when the proposer opt-in is off. Injected via SetSuggestionSink; not safe to call
	// concurrently with OnGameEnd.
	suggestionSink func(llm.RetroSuggestion)

	// inferWG tracks every in-flight retro inference goroutine (#1179). OnGameEnd is
	// a lifecycle hook and MUST NOT block on the 2-10s network call, so the
	// infer+decode+capture runs in its own goroutine; this wait group lets graceful
	// shutdown and tests join all in-flight retros (AwaitInflight) so no goroutine
	// outlives the process and tests can deterministically wait for the conclusion to
	// land before asserting — mirroring the in-game Loop.inferWG.
	inferWG sync.WaitGroup
	// rootCtx is the agent's root context (#1179): the retro inference goroutine bridges
	// rootCtx.Done() to its (span-derived) budget context's cancel, so shutdown
	// cancellation promptly unblocks a well-behaved in-flight Infer. Nil means no bridge
	// — the latency budget alone bounds the call.
	rootCtx context.Context

	// mu guards the captured-session dedupe state.
	mu sync.Mutex
	// captured is the set of recently captured SessionIDs (membership = already
	// captured); capturedOrder is the LRU eviction order (oldest first). Bounded by
	// retroDedupeWindow so interleaved concurrent game-ends each capture exactly
	// once without unbounded growth.
	captured      map[string]struct{}
	capturedOrder []string
}

// NewRetroCoordinator builds a RetroCoordinator over the given flag source. log
// may be nil (slog.Default() is used); flagSource may be nil, in which case the
// disabled fallback source is used (so a missing control plane never captures).
func NewRetroCoordinator(flagSource FlagSource, log *slog.Logger) *RetroCoordinator {
	if log == nil {
		log = slog.Default()
	}
	if flagSource == nil {
		flagSource = disabledFlags{}
	}
	return &RetroCoordinator{
		Tracer:   otel.Tracer(instrumentationName),
		Log:      log,
		Flags:    flagSource,
		now:      time.Now,
		captured: make(map[string]struct{}),
		llmGated: defaultLLMGatedCounter(),
	}
}

// SetLLMBudget injects the shared GLOBAL LLM request budget (#847 acceptance #7)
// so a retrospective draws one slot from the SAME per-minute cap the in-game gate
// uses. main.go passes the one instance every per-game Loop also holds. Not safe
// to call concurrently with OnGameEnd — set it during construction.
func (rc *RetroCoordinator) SetLLMBudget(b *llmBudget) { rc.budget = b }

// SetResolver injects the SHARED inference fallback-chain resolver (#1080), the same
// instance every per-game Loop holds (SetResolver on the Loop). The retro then routes
// its inference attribution through the SAME configured backend the decision loop and
// proposer use: when AGENT_INFERENCE_BACKEND=openai the resolver's configured tier
// (AGENT_INFERENCE_MODEL @ AGENT_INFERENCE_BASE_URL) is the canonical path, so retro
// no longer reports the unreachable phi4-mini legacy tier. Not safe to call
// concurrently with OnGameEnd — set it during construction.
func (rc *RetroCoordinator) SetResolver(r *Resolver) { rc.resolver = r }

// SetFitnessLookup injects the recorded-fitness lookup seam (#1100): a func that
// returns a finished game's settled objective fitness by game_id (reading the #965
// FitnessStore) and whether one is available. When wired and a sample exists at
// retro time, the retro stamps experiment.fitness on the span so a retro joins to
// the fitness its arm scored. A nil lookup (the default) means experiment.fitness
// is never emitted — default-safe, the span is unchanged but for the additive
// outcome attrs. Not safe to call concurrently with OnGameEnd — set it during
// construction.
func (rc *RetroCoordinator) SetFitnessLookup(f func(gameID string) (float64, bool)) {
	rc.fitnessLookup = f
}

// SetExperimentParent injects the experiment ROOT-span SpanContext lookup seam
// (#1188): a func that returns the agent.experiment span's SpanContext for an
// experiment_id (reading the registry's ExperimentSpanContext) and whether one is
// available. When wired and the finished game is experiment-bound with a live root
// span, the agent.llm.retro span is re-parented as a remote CHILD of the experiment
// span (joining the experiment's trace) instead of the pre-#1188 game-linked own-root
// span. A nil lookup (the default), a non-experiment game, or a torn-down experiment
// falls back to the existing behavior. Not safe to call concurrently with OnGameEnd —
// set it during construction.
// SetSuggestionSink injects the downstream consumer of decoded retro suggestions
// (#1184) — the experiment loop's retro-seeded thesis proposer in production. Each
// suggestion a post-game retro decodes is forwarded to the sink (in addition to
// being stamped as a span event). nil leaves the retro recorded-only (pre-#1184
// behavior). Not safe to call concurrently with OnGameEnd — set it during
// construction.
func (rc *RetroCoordinator) SetSuggestionSink(f func(llm.RetroSuggestion)) { rc.suggestionSink = f }

func (rc *RetroCoordinator) SetExperimentParent(f func(experimentID string) (trace.SpanContext, bool)) {
	rc.experimentParent = f
}

// SetRootContext injects the agent's root context (#1179). The async retro inference
// goroutine keeps its latency-budget context derived from the retro SPAN context (to
// preserve #1112 trace parenting), and separately bridges root.Done() -> cancel() so
// shutdown cancellation promptly unblocks a well-behaved in-flight Infer instead of
// waiting out the full budget. Nil (the default) means no bridge is installed — the
// latency budget is the sole deadline. Not safe to call concurrently with OnGameEnd —
// set it during construction.
func (rc *RetroCoordinator) SetRootContext(ctx context.Context) { rc.rootCtx = ctx }

// AwaitInflight blocks until every in-flight async retro inference has completed
// (#1179). main.go calls it during graceful shutdown so no retro goroutine outlives
// the process; tests call it to make the conclusion land deterministically before
// asserting (no real sleeps). Safe to call when nothing is in flight.
func (rc *RetroCoordinator) AwaitInflight() { rc.inferWG.Wait() }

// OnGameEnd is the gamecontext.Store.OnGameEnd callback. It captures the
// retrospective prompt for a finished session exactly once. It is defensive:
//   - skips if the snapshot still reports GameActive (only end-of-game captures);
//   - skips if SessionID is empty (nothing to attribute the retro to);
//   - skips if SessionID == the last captured session (exactly-once per session).
func (rc *RetroCoordinator) OnGameEnd(c gamecontext.GameContext) {
	if c.Session.GameActive != nil && *c.Session.GameActive {
		return // not actually a game end
	}
	if c.SessionID == "" {
		return
	}
	if !rc.claimSession(c.SessionID) {
		return // already captured this session
	}

	now := rc.now
	if now == nil {
		now = time.Now
	}

	// Evaluate flags with a background context: there is no inbound RPC context at
	// game end, so the retro span is a standalone root (intentional — it is not a
	// child of any Export). The flag source falls back to safe defaults if flagd is
	// unreachable, identical to the in-game path. Evaluated up front so the budget
	// gate below reads the same live llm.max_requests_per_minute the capture would.
	snapshot := rc.Flags.Evaluate(context.Background())

	// Global LLM budget gate (#847 acceptance #7): a retrospective is one LLM
	// request, so it must fit the shared per-minute cap the in-game gate honors.
	// When a budget is wired and exhausted, the retro is GATED — record the gated
	// metric and capture nothing (no prompt build, no span). A nil budget (tests /
	// unshared wiring) skips this check, preserving pre-#847 behavior.
	if rc.budget != nil && !rc.budget.allow(now(), snapshot.LLMGate.MaxRequestsPerMinute) {
		rc.llmGated.Add(context.Background(), 1,
			otelmetric.WithAttributes(attribute.String("reason", FallbackBudgetExhausted)))
		rc.Log.Info("agent.llm.retro_gated",
			"session_id", c.SessionID,
			"fallback_reason", FallbackBudgetExhausted,
		)
		return
	}

	// #1158: build the per-game narrative Summary from the SAME finished-game
	// snapshot and feed it to the retro, so the prompt renders the rich per-player
	// PlayerArc stanzas (survival / elimination-offset / movement sparkline /
	// activity / near-death) the agent already derives — not a flat terminal
	// snapshot. We call gamesummary.BuildSummary DIRECTLY rather than reusing the
	// gamesummary.Coordinator's Observer seam: that seam is a one-way PUSH sink
	// (Observer.Record(Summary)) the Coordinator fires to a registered window store,
	// with no way to pull the Summary back here, and the retro path holds no
	// reference to the Coordinator. BuildSummary is a documented PURE fold over the
	// snapshot (no clock/disk/network), so calling it here with the same injected
	// now() is cheap and deterministic, and keeps the retro self-contained — the two
	// once-per-game components stay decoupled. The injected now() matches BuildRetro's
	// Now so the Summary's GeneratedAt and the prompt's captured_at agree.
	retroNow := now()
	summary := gamesummary.BuildSummary(c, gamesummary.BuildOptions{Now: retroNow})

	prompt := llm.BuildRetro(llm.RetroInput{
		Snapshot: snapshot,
		Context:  c,
		Summary:  summary,
		Now:      retroNow,
	})

	// #1080: resolve the inference attribution through the SAME shared resolver the
	// decision loop and proposer use, so a configured backend (gemma4) is reported as
	// the model/tier — not the agent.model flag's unreachable phi4-mini legacy tier.
	// retroInference falls back to the flag-only "none"/no_backend_available attribution
	// when no resolver is wired (the pre-#1080 default), keeping that path identical.
	attr := rc.retroInference(snapshot.Capability.Model)
	// Attribute the prompt to the model that WOULD serve (the configured backend when
	// one is wired), overriding the flag-derived BuildRetro model so the span's
	// gen_ai.request.model and the log line both name the configured model.
	prompt.Model = attr.configured

	// #1168: the retro span carries only the System-prompt fingerprint; emit the full
	// text once per distinct fingerprint on the shared agent.llm.system_prompt
	// reference log (the same singleton the in-game capture uses) so the hash stays
	// resolvable to its text without paying per span (the #1167 OOM). The retro system
	// prompt is its own variant of the constant text, so it hashes distinctly from the
	// in-game one and is emitted once on its own first sight.
	systemPromptRef.emit(rc.Log, systemPromptSHA(prompt.System),
		snapshot.Capability.PromptVariant, retroModeValue, allowedSummary(snapshot.InterventionsAllowed), prompt.System)

	attrs := retroPromptAttributes(retroPromptAttrs{
		prompt:     prompt,
		sessionID:  c.SessionID,
		gameKind:   c.GameKind,
		objectives: snapshot.Objectives,
		allowed:    snapshot.InterventionsAllowed,
		configured: attr.configured,
		used:       attr.used,
		fallback:   attr.fallback,
	})
	// #1100: structured game-outcome attributes (queryable, not buried in the prompt
	// text) and shadow-experiment correlation (id / arm / recorded fitness). All are
	// additive and cleanly OMITTED when their signal is absent (a non-experiment game,
	// an unobserved outcome field, no fitness lookup wired), so the schema never emits
	// empty/garbage tags and the default path is unchanged.
	attrs = append(attrs, rc.outcomeAttributes(c)...)
	attrs = append(attrs, rc.experimentAttributes(c)...)

	// #1140 Slice B: the retro is post-game / async, so it stays a ROOT span (no
	// parent — there is no inbound RPC context at game end), but it is no longer
	// ORPHANED from the game it reflects on. When the finished session carries a
	// valid game-coordinator trace_id (GameContext.GameTraceID, #1133), add the SAME
	// span Link the agent.decision span uses (gameTraceLink, package-level in this
	// package), so agent.llm.retro is navigable to the originating game trace in
	// Jaeger. The trace_id is also stamped as a plain span attribute (mirroring
	// runDecision) so it stays queryable as a tag on backends that don't surface Link
	// attributes in search. An empty/invalid id yields no Link and no attribute —
	// graceful fallback, the span is byte-identical to before.
	startOpts := []trace.SpanStartOption{trace.WithAttributes(attrs...)}
	startOpts = append(startOpts, gameTraceLink(c.GameTraceID, c.GameTraceSpanID)...)
	if c.GameTraceID != "" {
		startOpts = append(startOpts,
			trace.WithAttributes(attribute.String(AttrGameTraceID, c.GameTraceID)))
	}

	// #1188: for an experiment-bound game, re-parent the retro under the experiment
	// ROOT span — the retro is "in between" games and the experiment outlives them, so
	// it joins the experiment's trace as a child. The game Link above is RETAINED so the
	// retro stays navigable to its originating game too. A non-experiment game / unwired
	// lookup / torn-down experiment yields an invalid SpanContext, so retroParentCtx
	// returns context.Background(): the pre-#1188 game-linked own-root span, unchanged.
	parent := rc.retroParentCtx(c.ExperimentID)
	spanCtx, span := rc.Tracer.Start(parent, SpanLLMRetro, startOpts...)

	rc.Log.Info("agent.llm.retro_captured",
		"session_id", c.SessionID,
		"model", attr.configured,
		"bytes", len(prompt.System)+len(prompt.User),
		"fallback_reason", attr.fallback,
	)

	// #1179: actually CALL the model. When a real backend resolved, ask it for the
	// post-game conclusion and stamp the decoded assessment/suggestions/focus on the
	// SAME span — which now ends AFTER the response (real latency), not at 10µs. When
	// no backend resolved (chain unreachable / no resolver wired), keep the pre-#1179
	// capture-only behavior: end the span now with fallback=no_backend_available.
	if attr.backend == nil {
		span.End()
		return
	}

	// ASYNC: OnGameEnd is a lifecycle hook on the game-end transition — it must NOT
	// block on the 2-10s inference call. Fire the infer+decode+capture in its own
	// goroutine (mirroring the in-game runInfer/applyAsyncResult split) and return
	// immediately; the span ends inside the goroutine once the response lands. Tracked
	// on inferWG so shutdown/tests can join it (AwaitInflight).
	budget := retroLatencyBudget(snapshot)
	rc.inferWG.Add(1)
	go rc.runRetroInfer(spanCtx, span, attr.backend, prompt, c.SessionID, budget)
}

// retroLatencyBudget is the wall-clock cap for one retro inference call (#1179),
// reusing the SAME llm.latency_budget_seconds flag the in-game async path uses so a
// retro can never outlive the configured budget. A non-positive value (a hand-built
// test snapshot) floors at the default — a retro must never run without a deadline, or
// a hung Infer would leak its goroutine and pin the span open forever.
func retroLatencyBudget(snapshot flags.Snapshot) time.Duration {
	budget := snapshot.LLMGate.LatencyBudget
	if budget <= 0 {
		budget = time.Duration(flags.DefaultLLMLatencyBudgetSeconds * float64(time.Second))
	}
	return budget
}

// runRetroInfer is the body of the fired retro goroutine (#1179): it calls Infer under
// the latency-budget context, decodes the reply, stamps the conclusion (or the failure)
// on the agent.llm.retro span, and ends the span. It runs OFF the game-end hook, so it
// may take seconds without blocking the lifecycle transition.
//
// FAIL-OPEN: it is total and never panics out — a flagd/backend/timeout/decode failure
// stamps the error on the span (parse_ok=false + llm.infer.error) and emits a WARN log,
// but the span always ends cleanly. OnGameEnd has already returned by the time this
// runs, so nothing here can break the game-end transition.
func (rc *RetroCoordinator) runRetroInfer(ctx context.Context, span trace.Span, backend Backend, prompt llm.RetroPrompt, sessionID string, budget time.Duration) {
	defer rc.inferWG.Done()
	defer span.End()

	now := rc.now
	if now == nil {
		now = time.Now
	}

	// inferCtx MUST stay derived from ctx (the live retro span context), NOT from
	// rootCtx: #1112 requires the outbound-call client span below to be parented under
	// THIS retro span ("one trace" through the litellm gateway), and that parenting flows
	// through ctx. WithTimeout(ctx, budget) gives us the span parent plus the latency
	// budget. (The sibling in-game path async_infer.go:232 derives from root directly
	// because it carries no span-parent-from-ctx constraint — do not copy that here.)
	inferCtx, cancel := context.WithTimeout(ctx, budget)
	defer cancel()

	// Linked cancellation: WithTimeout(ctx, budget) does NOT inherit rootCtx's
	// cancellation (ctx is the standalone retro root, not a child of root), so on
	// shutdown an in-flight Infer would otherwise wait out the full ~8s budget. Bridge
	// root.Done() -> cancel() with a small goroutine so shutdown promptly unblocks a
	// well-behaved Infer; the goroutine also exits when inferCtx finishes normally, so it
	// never leaks. A nil rootCtx (tests / unwired) skips the bridge — the budget timeout
	// remains the sole deadline.
	if root := rc.rootCtx; root != nil {
		go func() {
			select {
			case <-root.Done():
				cancel()
			case <-inferCtx.Done():
			}
		}()
	}

	// Wrap the outbound call in a short-lived client span (mirroring async_infer.go's
	// SpanLLMInferCall) so callCtx carries an active, sampled span for the #1112
	// traceparent injector to read — restoring "one trace" through the litellm gateway.
	callCtx, callSpan := rc.Tracer.Start(inferCtx, SpanLLMInferCall,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("gen_ai.request.model", backend.Name()),
			attribute.String(AttrGameID, sessionID),
			attribute.String(AttrSessionID, sessionID),
		),
	)
	start := now()
	// The retro prompt's System/User map onto the inference Prompt's System/User;
	// Model rides for attribution (already stamped on the span). The retro has no
	// in-game Variant.
	raw, inferErr := backend.Infer(callCtx, llm.Prompt{
		System: prompt.System,
		User:   prompt.User,
		Model:  prompt.Model,
	})
	callSpan.End()
	latencyMs := now().Sub(start).Milliseconds()

	span.SetAttributes(attribute.Int64(AttrLLMRetroLatencyMs, latencyMs))

	// Transport / timeout failure: stamp the error, log loudly, end (fail-open).
	if inferErr != nil {
		span.SetAttributes(
			attribute.Bool(AttrLLMRetroParseOK, false),
			attribute.String(AttrLLMInferError, inferErr.Error()),
		)
		// #1206: surface the failure as span STATUS=Error (mirroring litellm's
		// otel.status_code=ERROR) so a timed-out/failed retro is visible in Jaeger's
		// error view, not just as parse_ok=false. Fail-open is unchanged — the span
		// still ends cleanly and the game-end transition is unaffected.
		span.RecordError(inferErr)
		span.SetAttributes(semconv.ErrorType(inferErr))
		span.SetStatus(codes.Error, inferErr.Error())
		rc.Log.Warn(SpanLLMRetroFailed,
			"session_id", sessionID,
			"latency_ms", latencyMs,
			"error", inferErr.Error(),
		)
		return
	}

	// Keep the big raw text off the traces pipeline (#1169): stamp only the
	// fingerprint, emit the full text once-per-hash on the reference log.
	sha := responseSHA(raw)
	span.SetAttributes(attribute.String(AttrLLMRetroResponseSHA, sha))
	retroResponseRef.emit(rc.Log, sha, sessionID, raw)

	resp, decodeErr := llm.DecodeRetro(raw)
	if decodeErr != nil {
		span.SetAttributes(
			attribute.Bool(AttrLLMRetroParseOK, false),
			attribute.String(AttrLLMInferError, decodeErr.Error()),
		)
		// #1206: an unparseable retro reply is an ERROR on the span, not just
		// parse_ok=false. Fail-open is unchanged — the span still ends cleanly.
		span.RecordError(decodeErr)
		span.SetAttributes(semconv.ErrorType(decodeErr))
		span.SetStatus(codes.Error, decodeErr.Error())
		rc.Log.Warn(SpanLLMRetroFailed,
			"session_id", sessionID,
			"latency_ms", latencyMs,
			"error", decodeErr.Error(),
		)
		return
	}

	// Success: stamp the decoded conclusion on the span and emit one queryable event
	// per suggestion.
	span.SetAttributes(
		attribute.Bool(AttrLLMRetroParseOK, true),
		attribute.String(AttrLLMRetroSessionAssessment, resp.SessionAssessment),
		attribute.String(AttrLLMRetroSessionFocus, resp.SessionFocus),
		attribute.Int(AttrLLMRetroSuggestionCount, len(resp.Suggestions)),
	)
	for _, s := range resp.Suggestions {
		span.AddEvent(SpanLLMRetroSuggestion, trace.WithAttributes(
			attribute.String(AttrRetroSuggestionType, s.InterventionType),
			attribute.String(AttrRetroSuggestionEmphasis, s.Emphasis),
			attribute.String(AttrRetroSuggestionReason, s.Reason),
		))
		// #1184: forward the decoded suggestion to the downstream consumer (the
		// retro-seeded thesis proposer) so it can seed the next experiment's thesis,
		// closing the game→retro→thesis→experiment loop. Additive — the suggestion is
		// already recorded as the span event above; a nil sink (proposer not wired)
		// leaves the retro recorded-only.
		if rc.suggestionSink != nil {
			rc.suggestionSink(s)
		}
	}
}

// retroAttribution is the inference attribution for one retro capture: the
// configured model (gen_ai.request.model / inference.configured), the tier that
// WOULD serve (inference.used), and any degradation reason (#1080).
type retroAttribution struct {
	configured string
	used       string
	fallback   string
	// backend is the LIVE resolved Backend the retro should actually call Infer on
	// (#1179) — the same instance the resolver handed back, surfaced here instead of
	// discarded. Nil when the whole chain was unreachable (or no resolver is wired):
	// the retro then stays capture-only with fallback=no_backend_available.
	backend Backend
}

// retroInference computes the retro span/log inference attribution (#1080), routing
// through the SHARED resolver so it matches the decision loop's configured backend.
//
//   - No resolver wired (tests / stub-default without one): preserve the pre-#1080
//     attribution exactly — configured = the agent.model flag, used = "none"
//     (DefaultInference: a retro has no rules fallback, see the DIVERGENCE note on
//     retroPromptAttributes), fallback = no_backend_available.
//   - Resolver wired: configured = the configured inference model when one is set
//     (AGENT_INFERENCE_MODEL), else the flag. used/fallback come from resolving the
//     SAME availability cache the decision loop reads:
//   - configured tier reachable -> used = the tier, fallback = "" (a real backend
//     WOULD serve the retro — no degradation);
//   - a lower legacy tier serves -> used = that tier, fallback = endpoint_unreachable;
//   - whole chain unreachable    -> used = "none" (DefaultInference, NOT "rules":
//     the retro DIVERGENCE — no rules fallback at game end), fallback =
//     no_backend_available — byte-identical to the legacy stub path.
func (rc *RetroCoordinator) retroInference(flagModel string) retroAttribution {
	if rc.resolver == nil {
		return retroAttribution{
			configured: flagModel,
			used:       DefaultInference,
			fallback:   FallbackNoBackend,
		}
	}
	configured := rc.resolver.ConfiguredModel()
	if configured == "" {
		configured = flagModel
	}
	res := rc.resolver.ResolveConfigured(flagModel)
	// A nil Backend means the whole chain was unreachable and the decision loop would
	// fall back to rules; the retro has no rules fallback, so it reports used="none"
	// (the DIVERGENCE) with the chain's no_backend_available reason — identical to the
	// pre-#1080 stub attribution. The nil backend is surfaced (#1179) so OnGameEnd keeps
	// the capture-only path with no_backend_available.
	if res.Backend == nil {
		return retroAttribution{
			configured: configured,
			used:       DefaultInference,
			fallback:   res.FallbackReason,
		}
	}
	// #1179: surface the LIVE Backend so OnGameEnd can actually call Infer on it,
	// instead of resolving the attribution and discarding the resolved tier.
	return retroAttribution{
		configured: configured,
		used:       res.Used,
		fallback:   res.FallbackReason,
		backend:    res.Backend,
	}
}

// claimSession records sessionID as captured and reports whether THIS call won
// the claim (i.e. the session had not been captured yet). Mutex-guarded for the
// exactly-once guarantee. It remembers the last retroDedupeWindow sessions (LRU),
// so interleaved concurrent game-ends (#845 PR B) each capture exactly once: a
// repeated end for ANY remembered session is suppressed, not just the most recent.
func (rc *RetroCoordinator) claimSession(sessionID string) bool {
	rc.mu.Lock()
	defer rc.mu.Unlock()
	if _, ok := rc.captured[sessionID]; ok {
		return false
	}
	rc.captured[sessionID] = struct{}{}
	rc.capturedOrder = append(rc.capturedOrder, sessionID)
	if len(rc.capturedOrder) > retroDedupeWindow {
		oldest := rc.capturedOrder[0]
		rc.capturedOrder = rc.capturedOrder[1:]
		delete(rc.captured, oldest)
	}
	return true
}

// retroPromptAttrs bundles the inputs for the single retro span-attribute
// builder, mirroring llmPromptAttrs.
type retroPromptAttrs struct {
	prompt     llm.RetroPrompt
	sessionID  string
	gameKind   string
	objectives map[string]float64
	allowed    []string
	// configured/used/fallback are the #1080 inference attribution resolved through
	// the shared resolver: the configured backend model, the tier that WOULD serve,
	// and any degradation reason. They replace the formerly hard-coded
	// model-flag/"none"/no_backend_available trio.
	configured string
	used       string
	fallback   string
}

// retroPromptAttributes is the SOLE builder of the agent.llm.retro span attribute
// set (#844), mirroring llmPromptAttributes. Every emission routes through it so
// the schema is complete and consistent on every emission:
//
//   - gen_ai.operation.name = "chat"        (semconv)
//   - gen_ai.request.model  = <model flag>  (semconv)
//   - gen_ai.output.type    = "json"        (semconv key; JSON per RESPONSE CONTRACT)
//   - agent.mode            = "retro"
//   - agent.objectives      = sorted "k=v" weights (summarizeObjectives)
//   - interventions.allowed = the allow-list summary (allowedSummary)
//   - session.id / game.id  = the finished session's id (= the real game_id, #1088)
//   - llm.retro.system_sha256 = the System-prompt FINGERPRINT (#1168), NOT the text
//   - llm.retro.user        = the User prompt text (capped at maxUserPromptBytes)
//   - llm.retro.bytes       = len(system)+len(user) (full size; system TEXT not emitted)
//   - inference.configured  = the configured backend model (#1080), or the model flag
//   - inference.used        = the tier that WOULD serve, or "none" (see DIVERGENCE)
//   - inference.fallback_reason = "" when the configured backend is reachable, else
//     endpoint_unreachable / no_backend_available
//
// #1080: configured/used/fallback are resolved through the SHARED resolver (the same
// one the decision loop uses), so when AGENT_INFERENCE_BACKEND=openai the retro names
// the configured model (gemma4) instead of the agent.model flag's unreachable
// phi4-mini legacy tier. With no resolver wired the legacy flag-only attribution is
// preserved exactly (see retroInference).
//
// DIVERGENCE from the in-game capture: when nothing is reachable, inference.used is
// "none", NOT "rules". The in-game path falls back to the rules engine, so "rules" is
// the honest answer for "what decided this cycle". A retrospective has NO rules
// fallback — nothing runs in place of the offline analyst at game end, so claiming
// "rules" would be a lie. "none" (DefaultInference) is the honest value: no inference
// of any kind ran. A reachable configured backend (#1080) reports used=<that tier>.
func retroPromptAttributes(in retroPromptAttrs) []attribute.KeyValue {
	system := in.prompt.System
	user := in.prompt.User
	return []attribute.KeyValue{
		// GenAI semantic conventions for the request the agent would have sent.
		semconv.GenAIOperationNameChat,
		semconv.GenAIRequestModel(in.prompt.Model),
		genAIOutputTypeJSON,
		// Agent attribution (same vocabulary as the in-game capture / decision span).
		attribute.String(AttrMode, retroModeValue),
		attribute.String(AttrGameKind, in.gameKind),
		attribute.String(AttrObjectives, summarizeObjectives(in.objectives)),
		attribute.String(AttrInterventionsAllowed, allowedSummary(in.allowed)),
		// session.id + game.id (#1088): the post-game retro span is attributable to the
		// finished game like the in-game agent.llm.prompt span, so a Jaeger query by
		// game.id surfaces the retrospective alongside the game's in-play decisions.
		// game.id IS the SessionID (= the real game_id since #845 PR A).
		attribute.String(AttrSessionID, in.sessionID),
		attribute.String(AttrGameID, in.sessionID),
		// #1168: the System prompt rides as a FINGERPRINT (resolvable via the
		// once-per-hash agent.llm.system_prompt reference log), NOT the full text. The
		// User prompt (variable, valuable) is KEPT, capped at maxUserPromptBytes. bytes
		// still counts the full system+user size.
		attribute.String(AttrLLMRetroSystemSHA, systemPromptSHA(system)),
		attribute.String(AttrLLMRetroUser, capUserPrompt(user)),
		attribute.Int(AttrLLMRetroBytes, len(system)+len(user)),
		// Inference attribution (#1080): resolved through the shared resolver so the
		// configured backend (gemma4) is named when one is wired; used is the tier that
		// WOULD serve (or "none" when nothing is reachable — the retro DIVERGENCE), and
		// fallback is empty when the configured backend is reachable.
		attribute.String(AttrInferenceConfigured, in.configured),
		attribute.String(AttrInferenceUsed, in.used),
		attribute.String(AttrInferenceFallback, in.fallback),
	}
}

// outcomeAttributes builds the STRUCTURED game-outcome span attributes (#1100):
// winner / final-active-players / elimination-count / duration as DISCRETE,
// queryable tags. They are derived from the same already-structured GameContext
// fields the retro prompt renders its Outcome block from (llm.DeriveRetroOutcome —
// the SINGLE source of truth shared with the prompt), so the span and the prompt
// text can never disagree, and NOTHING is re-parsed out of the prompt. Each
// optional field is omitted when its signal was never observed (winner ambiguous,
// active-count/duration nil) so the span carries no misleading zeros. The
// elimination count is always present (a finished game has a definite, possibly
// empty, elimination sequence).
func (rc *RetroCoordinator) outcomeAttributes(c gamecontext.GameContext) []attribute.KeyValue {
	o := llm.DeriveRetroOutcome(c)
	out := make([]attribute.KeyValue, 0, 4)
	if o.Winner != "" {
		out = append(out, attribute.String(AttrGameWinner, o.Winner))
	}
	if o.FinalActivePlayers != nil {
		out = append(out, attribute.Int(AttrGameFinalActivePlayers, *o.FinalActivePlayers))
	}
	out = append(out, attribute.Int(AttrGameEliminationCount, o.EliminationCount))
	if o.DurationSeconds != nil {
		out = append(out, attribute.Float64(AttrGameDurationSeconds, *o.DurationSeconds))
	}
	return out
}

// experimentAttributes builds the shadow-EXPERIMENT correlation span attributes
// (#1100): experiment.id + experiment.arm when the finished game was an experiment
// arm, plus experiment.fitness when a recorded fitness sample is available at retro
// time via the injected lookup seam. The id / arm come straight off the GameContext
// (the cohort enrichment carried on every lifecycle signal — extract_spans.go), so
// no registry seam is needed and the lookup never races the registry's conclude-time
// binding deletion. For a REAL / standalone game (ExperimentID == "") this returns
// NOTHING — the correlation attrs are absent entirely, never emitted empty. Fitness
// is stamped only when a lookup is wired AND a settled sample exists; otherwise it
// is simply omitted (default-safe).
func (rc *RetroCoordinator) experimentAttributes(c gamecontext.GameContext) []attribute.KeyValue {
	if c.ExperimentID == "" {
		return nil // not part of an experiment: emit no experiment.* attrs.
	}
	out := make([]attribute.KeyValue, 0, 3)
	out = append(out, attribute.String(AttrExperimentID, c.ExperimentID))
	if c.Arm != "" {
		out = append(out, attribute.String(AttrExperimentArm, c.Arm))
	}
	if rc.fitnessLookup != nil {
		if fitness, ok := rc.fitnessLookup(c.SessionID); ok {
			out = append(out, attribute.Float64(AttrExperimentFitness, fitness))
		}
	}
	return out
}

// retroParentCtx returns the context the agent.llm.retro span is started from (#1188):
// a context whose remote parent is the experiment ROOT span for an experiment-bound
// game with a live root span, else plain context.Background() (the pre-#1188 own-root
// behavior). It reads the SpanContext via the injected experimentParent seam and installs
// it with gamecontext.RemoteParentFromSpanContext.
//
// GRACEFUL FALLBACK: an empty experiment_id (non-experiment game), a nil seam (the
// default / tests), or ok=false (the experiment has no live root span — torn down, or
// tracer disabled) all yield context.Background(), so the retro span is byte-identical
// to before #1188 and emission is never broken.
func (rc *RetroCoordinator) retroParentCtx(experimentID string) context.Context {
	base := context.Background()
	if experimentID == "" || rc.experimentParent == nil {
		return base
	}
	sc, ok := rc.experimentParent(experimentID)
	if !ok {
		return base
	}
	ctx, _ := gamecontext.RemoteParentFromSpanContext(base, sc)
	return ctx
}

// compile-time guard: RetroCoordinator.OnGameEnd matches the store hook signature.
var _ func(gamecontext.GameContext) = (*RetroCoordinator)(nil).OnGameEnd
