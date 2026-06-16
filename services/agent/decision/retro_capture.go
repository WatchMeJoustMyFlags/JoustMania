package decision

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
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
	// AttrLLMRetroSystem / AttrLLMRetroUser carry the full, uncapped System and
	// User retrospective prompt text. As with the in-game capture, the Go SDK
	// applies no attribute length limit and the collector does not truncate, so the
	// entire prompt is preserved for offline replay (scripts/replay-prompt.sh).
	AttrLLMRetroSystem = "llm.retro.system"
	AttrLLMRetroUser   = "llm.retro.user"
	// AttrLLMRetroBytes is len(system)+len(user), the prompt size in bytes (int).
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

	prompt := llm.BuildRetro(llm.RetroInput{
		Snapshot: snapshot,
		Context:  c,
		Now:      now(),
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
	startOpts = append(startOpts, gameTraceLink(c.GameTraceID)...)
	if c.GameTraceID != "" {
		startOpts = append(startOpts,
			trace.WithAttributes(attribute.String(AttrGameTraceID, c.GameTraceID)))
	}

	_, span := rc.Tracer.Start(context.Background(), SpanLLMRetro, startOpts...)
	span.End()

	rc.Log.Info("agent.llm.retro_captured",
		"session_id", c.SessionID,
		"model", attr.configured,
		"bytes", len(prompt.System)+len(prompt.User),
		"fallback_reason", attr.fallback,
	)
}

// retroAttribution is the inference attribution for one retro capture: the
// configured model (gen_ai.request.model / inference.configured), the tier that
// WOULD serve (inference.used), and any degradation reason (#1080).
type retroAttribution struct {
	configured string
	used       string
	fallback   string
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
	// pre-#1080 stub attribution.
	if res.Backend == nil {
		return retroAttribution{
			configured: configured,
			used:       DefaultInference,
			fallback:   res.FallbackReason,
		}
	}
	return retroAttribution{
		configured: configured,
		used:       res.Used,
		fallback:   res.FallbackReason,
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
//   - llm.retro.system / llm.retro.user = the FULL prompt text (uncapped)
//   - llm.retro.bytes       = len(system)+len(user)
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
		// The captured prompt: full text (uncapped) plus its size.
		attribute.String(AttrLLMRetroSystem, system),
		attribute.String(AttrLLMRetroUser, user),
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

// compile-time guard: RetroCoordinator.OnGameEnd matches the store hook signature.
var _ func(gamecontext.GameContext) = (*RetroCoordinator)(nil).OnGameEnd
