package flags

import (
	"context"
	"strings"
	"time"
)

// Agent tuning settings migrated from env to agent.json flags (#1788, FR-150,
// FR-151, flags.md §15). Each is read at USE time (every Infer call, every
// dispatched intervention, every promotion), so an operator write through the flag
// service takes effect on the next use with no restart.
//
// Resolution (FR-151, "the flag always wins"): the flag value when flagd resolves
// it; when it cannot be resolved (flagd unreachable, flag missing, no first sync:
// the evaluator returns an error) the caller's def, which main.go builds from the
// deprecated env var when set, else the compiled default. None of these limits
// the agent (C-034), so none fails closed. The typed getters are used (never
// ObjectValue: the #903 TYPE_MISMATCH class).
const (
	keyInferenceModel          = "inference.model"
	keyInferenceEffort         = "inference.effort"
	keyInferenceTimeoutSeconds = "inference.timeout_seconds"
	keyEffectWindowSeconds     = "effect.window_seconds"
	keyRewatchIntervalSeconds  = "promotion.rewatch_interval_seconds"
	keyRewatchTimeoutSeconds   = "promotion.rewatch_timeout_seconds"
	keyRevertCooldownSeconds   = "promotion.revert_cooldown_seconds"
)

// InferenceModel resolves inference.model (was AGENT_INFERENCE_MODEL). A blank
// value falls back to def.
func (f *Flags) InferenceModel(ctx context.Context, def string) string {
	if v := strings.TrimSpace(f.stringFlag(ctx, keyInferenceModel, def)); v != "" {
		return v
	}
	return def
}

// InferenceEffort resolves inference.effort (was AGENT_INFERENCE_EFFORT). A blank
// value falls back to def; the inference client validates the level.
func (f *Flags) InferenceEffort(ctx context.Context, def string) string {
	if v := strings.TrimSpace(f.stringFlag(ctx, keyInferenceEffort, def)); v != "" {
		return v
	}
	return def
}

// InferenceTimeout resolves inference.timeout_seconds (was
// AGENT_INFERENCE_TIMEOUT_SECONDS, int seconds).
func (f *Flags) InferenceTimeout(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyInferenceTimeoutSeconds, def)
}

// EffectWindow resolves effect.window_seconds (was AGENT_EFFECT_WINDOW_SECONDS, a
// float of seconds). A non-positive value falls back to def.
func (f *Flags) EffectWindow(ctx context.Context, def time.Duration) time.Duration {
	return f.durationFlag(ctx, keyEffectWindowSeconds, def.Seconds())
}

// RewatchInterval resolves promotion.rewatch_interval_seconds (was
// AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS).
func (f *Flags) RewatchInterval(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyRewatchIntervalSeconds, def)
}

// RewatchTimeout resolves promotion.rewatch_timeout_seconds (was
// AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS).
func (f *Flags) RewatchTimeout(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyRewatchTimeoutSeconds, def)
}

// RevertCooldown resolves promotion.revert_cooldown_seconds (was
// AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS).
func (f *Flags) RevertCooldown(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyRevertCooldownSeconds, def)
}

// secondsFlag resolves an int "seconds" flag into a Duration, falling back to def
// on an evaluation error or a non-positive value.
func (f *Flags) secondsFlag(ctx context.Context, key string, def time.Duration) time.Duration {
	secs := f.intFlag(ctx, key, int(def/time.Second))
	if secs <= 0 {
		f.log.Warn("flags seconds non-positive, using default", "key", key, "value", secs, "default", def)
		return def
	}
	return time.Duration(secs) * time.Second
}
