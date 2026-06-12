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
	// llmGated counts gated retro captures (agent_llm_gated_total{reason=...}),
	// sharing the in-game counter name. Defaults to a no-op so tests need not wire it.
	llmGated otelmetric.Int64Counter

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

	_, span := rc.Tracer.Start(context.Background(), SpanLLMRetro,
		trace.WithAttributes(retroPromptAttributes(retroPromptAttrs{
			prompt:     prompt,
			sessionID:  c.SessionID,
			gameKind:   c.GameKind,
			objectives: snapshot.Objectives,
			allowed:    snapshot.InterventionsAllowed,
		})...))
	span.End()

	rc.Log.Info("agent.llm.retro_captured",
		"session_id", c.SessionID,
		"model", prompt.Model,
		"bytes", len(prompt.System)+len(prompt.User),
		"fallback_reason", FallbackNoBackend,
	)
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
//   - session.id            = the finished session's id
//   - llm.retro.system / llm.retro.user = the FULL prompt text (uncapped)
//   - llm.retro.bytes       = len(system)+len(user)
//   - inference.configured  = <model flag>
//   - inference.used        = "none"   (see DIVERGENCE below)
//   - inference.fallback_reason = "no_backend_available"
//
// DIVERGENCE from the in-game capture: inference.used is "none", NOT "rules".
// The in-game path falls back to the rules engine, so "rules" is the honest
// answer for "what decided this cycle". A retrospective has NO rules fallback —
// nothing runs in place of the offline analyst at game end, so claiming "rules"
// would be a lie. "none" (DefaultInference) is the honest value: no inference of
// any kind ran. #741's backend will supply used="llm" once it answers.
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
		attribute.String("session.id", in.sessionID),
		// The captured prompt: full text (uncapped) plus its size.
		attribute.String(AttrLLMRetroSystem, system),
		attribute.String(AttrLLMRetroUser, user),
		attribute.Int(AttrLLMRetroBytes, len(system)+len(user)),
		// Inference attribution: the prompt was captured, but no backend ran and
		// there is no rules fallback for a retrospective, so used="none".
		attribute.String(AttrInferenceConfigured, in.prompt.Model),
		attribute.String(AttrInferenceUsed, DefaultInference),
		attribute.String(AttrInferenceFallback, FallbackNoBackend),
	}
}

// compile-time guard: RetroCoordinator.OnGameEnd matches the store hook signature.
var _ func(gamecontext.GameContext) = (*RetroCoordinator)(nil).OnGameEnd
