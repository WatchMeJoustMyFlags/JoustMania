package decision

import (
	"context"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
)

// llm_admission.go is the shared LLM call gate for model calls made OUTSIDE a
// decision Loop (#1270): today the post-game experiment proposer. It applies the
// same two checks the retro applies at game end (retro_capture.go OnGameEnd), read
// LIVE from the flag snapshot on every call:
//
//   - eligibility: the triggering game kind must be in llm.eligible_game_kinds
//     (default ["real"]), so shadow-game ends never spend the budget that real-game
//     remote decide calls need (reason llm_not_eligible);
//   - budget: one slot of the ONE shared global per-minute budget
//     (llm.max_requests_per_minute, or the AGENT_LLM_MAX_REQUESTS_PER_MINUTE
//     override when set) that every per-game Loop and the retro also draw from
//     (reason llm_budget_exhausted).
//
// A refusal increments agent_llm_gated_total{reason} — the same counter and reason
// values as the decide path and the retro.

// LLMAdmission admits or gates one model call against the shared LLM call gate.
// Safe for concurrent use (the budget is mutex-guarded; the rest is read-only).
type LLMAdmission struct {
	flags          FlagSource
	budget         *llmBudget
	budgetOverride int
	llmGated       otelmetric.Int64Counter
	// gatedEvent is the log event name for a refusal: agent.llm.<source>_gated.
	gatedEvent string
	log        *slog.Logger
	now        func() time.Time
}

// NewLLMAdmission builds the gate over the live flag source and the shared budget
// (the instance main.go injects into every Loop and the retro). budgetOverride > 0
// replaces llm.max_requests_per_minute as the cap (AGENT_LLM_MAX_REQUESTS_PER_MINUTE,
// #964); 0 means the flag governs. A nil budget leaves the call ungated by the cap
// (eligibility still applies), mirroring the retro's nil-budget test wiring.
// source names the caller (e.g. "proposer") and sets the refusal log event to
// agent.llm.<source>_gated.
func NewLLMAdmission(src FlagSource, budget *llmBudget, budgetOverride int, source string, log *slog.Logger) *LLMAdmission {
	if log == nil {
		log = slog.Default()
	}
	if budgetOverride < 0 {
		budgetOverride = 0
	}
	return &LLMAdmission{
		flags:          src,
		budget:         budget,
		budgetOverride: budgetOverride,
		llmGated:       defaultLLMGatedCounter(),
		gatedEvent:     "agent.llm." + source + "_gated",
		log:            log,
		now:            time.Now,
	}
}

// budgetCap returns the per-minute cap: the override when set, else the flag value.
func (a *LLMAdmission) budgetCap(flagValue int) int {
	if a.budgetOverride > 0 {
		return a.budgetOverride
	}
	return flagValue
}

// Admit reports whether one model call for a game of gameKind may be made now. On
// admission it charges one budget slot. On refusal it records
// agent_llm_gated_total{reason} and returns the reason (FallbackNotEligible or
// FallbackBudgetExhausted). It satisfies experiment.LLMAdmission.
func (a *LLMAdmission) Admit(ctx context.Context, gameKind string) (bool, string) {
	gate := a.flags.Evaluate(ctx).LLMGate
	if !gate.EligibleFor(gameKind) {
		a.gated(FallbackNotEligible, gameKind)
		return false, FallbackNotEligible
	}
	if a.budget != nil && !a.budget.allow(a.now(), a.budgetCap(gate.MaxRequestsPerMinute)) {
		a.gated(FallbackBudgetExhausted, gameKind)
		return false, FallbackBudgetExhausted
	}
	return true, ""
}

func (a *LLMAdmission) gated(reason, gameKind string) {
	a.llmGated.Add(context.Background(), 1,
		otelmetric.WithAttributes(attribute.String("reason", reason)))
	a.log.Info(a.gatedEvent, "fallback_reason", reason, "game_kind", gameKind)
}
