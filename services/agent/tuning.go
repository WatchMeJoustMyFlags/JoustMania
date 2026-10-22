package main

import (
	"context"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/promote"
)

// tuning.go wires the agent tuning settings migrated from env to flags (#1788,
// FR-150, flags.md §15). Every value is resolved at USE time through the
// in-process flagd provider: the flag always wins; only when the flag cannot be
// resolved (flagd unreachable, flag missing, no first sync) the compiled default
// applies (#1907: the deprecated env fallbacks are removed).

// tuningFlagSource is the slice of *flags.Flags the tuning reads. Each method
// returns def when the flag cannot be resolved.
type tuningFlagSource interface {
	InferenceModel(ctx context.Context, def string) string
	InferenceEffort(ctx context.Context, def string) string
	InferenceTimeout(ctx context.Context, def time.Duration) time.Duration
	EffectWindow(ctx context.Context, def time.Duration) time.Duration
	RewatchInterval(ctx context.Context, def time.Duration) time.Duration
	RewatchTimeout(ctx context.Context, def time.Duration) time.Duration
	RevertCooldown(ctx context.Context, def time.Duration) time.Duration
}

// tuningDefaults are the fallbacks used when a flag cannot be resolved: the compiled
// defaults (compiledTuningDefaults).
type tuningDefaults struct {
	InferenceModel   string
	InferenceEffort  string
	InferenceTimeout time.Duration
	EffectWindow     time.Duration
	RewatchInterval  time.Duration
	RewatchTimeout   time.Duration
	RevertCooldown   time.Duration
}

// compiledTuningDefaults returns the fallbacks used while a flag cannot be resolved
// (flagd down, flag missing, no first sync). They are compile-time constants equal to
// the flag defaults in services/flagd/agent.json (pinned by
// TestCompiledDefaultsMatchFlagDefaults); no env var overrides them (#1907, C-035).
func compiledTuningDefaults() tuningDefaults {
	return tuningDefaults{
		InferenceModel:   inference.DefaultModel,
		InferenceEffort:  inference.DefaultEffort,
		InferenceTimeout: inference.DefaultTimeout,
		EffectWindow:     decision.DefaultEffectWindow,
		RewatchInterval:  promote.DefaultRewatchInterval,
		RewatchTimeout:   promote.DefaultRewatchTimeout,
		RevertCooldown:   promote.DefaultRevertCooldown,
	}
}

// agentTuning resolves each setting at use time. A nil src (no flagd wiring, unit
// tests) uses the defaults only.
type agentTuning struct {
	src tuningFlagSource
	def tuningDefaults
}

func newAgentTuning(src tuningFlagSource, def tuningDefaults) *agentTuning {
	return &agentTuning{src: src, def: def}
}

// Inference resolves the text-backend settings for one Infer call.
func (t *agentTuning) Inference(ctx context.Context) inference.Settings {
	if t.src == nil {
		return inference.Settings{Model: t.def.InferenceModel, Effort: t.def.InferenceEffort, Timeout: t.def.InferenceTimeout}
	}
	return inference.Settings{
		Model:   t.src.InferenceModel(ctx, t.def.InferenceModel),
		Effort:  t.src.InferenceEffort(ctx, t.def.InferenceEffort),
		Timeout: t.src.InferenceTimeout(ctx, t.def.InferenceTimeout),
	}
}

// InferenceTimeout resolves the per-call deadline (the retro uses it too).
func (t *agentTuning) InferenceTimeout() time.Duration {
	if t.src == nil {
		return t.def.InferenceTimeout
	}
	return t.src.InferenceTimeout(context.Background(), t.def.InferenceTimeout)
}

// EffectWindow resolves the intervention-effect follow-up window.
func (t *agentTuning) EffectWindow() time.Duration {
	if t.src == nil {
		return t.def.EffectWindow
	}
	return t.src.EffectWindow(context.Background(), t.def.EffectWindow)
}

// RewatchInterval resolves the autonomous re-watch poll interval.
func (t *agentTuning) RewatchInterval() time.Duration {
	if t.src == nil {
		return t.def.RewatchInterval
	}
	return t.src.RewatchInterval(context.Background(), t.def.RewatchInterval)
}

// RewatchTimeout resolves how long an autonomous re-watch waits.
func (t *agentTuning) RewatchTimeout() time.Duration {
	if t.src == nil {
		return t.def.RewatchTimeout
	}
	return t.src.RewatchTimeout(context.Background(), t.def.RewatchTimeout)
}

// RevertCooldown resolves the post-revert re-promotion cooldown.
func (t *agentTuning) RevertCooldown() time.Duration {
	if t.src == nil {
		return t.def.RevertCooldown
	}
	return t.src.RevertCooldown(context.Background(), t.def.RevertCooldown)
}

// llmBudgetCeiling reads the operator's ceiling on remote model calls per minute
// from AGENT_LLM_MAX_REQUESTS_PER_MINUTE (#1788, NFR-011, FR-152, C-034). It stays
// an env value: the agent can never write it. The enforced cap is
// min(ceiling, llm.max_requests_per_minute), so the flag can only tighten it. An
// unset, empty, invalid or non-positive value returns 0 = no ceiling (the flag
// governs).
func llmBudgetCeiling() int {
	s := strings.TrimSpace(os.Getenv("AGENT_LLM_MAX_REQUESTS_PER_MINUTE"))
	if s == "" {
		return 0
	}
	n, err := strconv.Atoi(s)
	if err != nil || n <= 0 {
		slog.Warn("AGENT_LLM_MAX_REQUESTS_PER_MINUTE invalid; no ceiling (flag governs) (#1788)", "value", s)
		return 0
	}
	return n
}
