package main

import (
	"context"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/promote"
)

// tuning.go wires the agent tuning settings migrated from env to flags (#1788,
// FR-150, FR-151, flags.md §15). Every value is resolved at USE time through the
// in-process flagd provider: the flag always wins; only when the flag cannot be
// resolved (flagd unreachable, flag missing, no first sync) the deprecated env var
// applies when set, else the compiled default. The env vars are read once at
// startup (the process environment does not change); they only supply the fallback.

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

// tuningDefaults are the fallbacks used when a flag cannot be resolved: the
// deprecated env var when set and valid, else the compiled default (FR-151).
type tuningDefaults struct {
	InferenceModel   string
	InferenceEffort  string
	InferenceTimeout time.Duration
	EffectWindow     time.Duration
	RewatchInterval  time.Duration
	RewatchTimeout   time.Duration
	RevertCooldown   time.Duration
}

// deprecatedTuningEnv lists the env vars FR-151 keeps as a fallback for one release.
var deprecatedTuningEnv = []string{
	"AGENT_INFERENCE_MODEL",
	"AGENT_INFERENCE_EFFORT",
	"AGENT_INFERENCE_TIMEOUT_SECONDS",
	"AGENT_EFFECT_WINDOW_SECONDS",
	"AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS",
	"AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS",
	"AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS",
}

// tuningDefaultsFromEnv resolves the fallbacks: each deprecated env var when set
// and valid, else the compiled default. A set deprecated var is logged at WARN so
// an operator sees that the flag overrides it once flagd answers.
func tuningDefaultsFromEnv(logger *slog.Logger) tuningDefaults {
	if logger == nil {
		logger = slog.Default()
	}
	for _, key := range deprecatedTuningEnv {
		if strings.TrimSpace(os.Getenv(key)) != "" {
			logger.Warn("deprecated env var set: used only when its agent flag cannot be resolved (#1788, FR-151)", "env", key)
		}
	}
	return tuningDefaults{
		InferenceModel:   getEnv("AGENT_INFERENCE_MODEL", inference.DefaultModel),
		InferenceEffort:  getEnv("AGENT_INFERENCE_EFFORT", inference.DefaultEffort),
		InferenceTimeout: secondsEnv("AGENT_INFERENCE_TIMEOUT_SECONDS", inference.DefaultTimeout),
		EffectWindow:     effectWindowFromEnv(),
		RewatchInterval:  secondsEnv("AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS", promote.DefaultRewatchInterval),
		RewatchTimeout:   secondsEnv("AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS", promote.DefaultRewatchTimeout),
		RevertCooldown:   secondsEnv("AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS", promote.DefaultRevertCooldown),
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
