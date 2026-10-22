package flags

import (
	"context"
	"strings"
	"time"
)

// Agent tuning settings migrated from env to agent.json flags (#1788, FR-150,
// C-035, flags.md §15). Each is read at USE time (every Infer call, every
// dispatched intervention, every promotion), so an operator write through the flag
// service takes effect on the next use with no restart.
//
// Resolution ("the flag always wins"): the flag value when flagd resolves it; when
// it cannot be resolved (flagd unreachable, flag missing, no first sync: the
// evaluator returns an error) the caller's def, the compiled default (the env
// fallbacks were removed in #1907, FR-151 Obsolete). None of these limits
// the agent (C-034), so none fails closed. The typed getters are used (never
// ObjectValue: the #903 TYPE_MISMATCH class).
const (
	keyInferenceBackend        = "inference.backend"
	keyInferenceModel          = "inference.model"
	keyInferenceEffort         = "inference.effort"
	keyInferenceTimeoutSeconds = "inference.timeout_seconds"
	keyEffectWindowSeconds     = "effect.window_seconds"
	keyProbeDecisions          = "decision.probe_decisions"
	keyRewatchIntervalSeconds  = "promotion.rewatch_interval_seconds"
	keyRewatchTimeoutSeconds   = "promotion.rewatch_timeout_seconds"
	keyRevertCooldownSeconds   = "promotion.revert_cooldown_seconds"
)

// InferenceBackend resolves inference.backend: the
// text backend of the post-game retro and the shadow-experiment proposer, "stub" or
// "claude". The value is lower-cased and trimmed; a blank value falls back to def
// (main.go passes the constant "stub"). Choosing a value that is
// not usable (claude without a key) is the caller's job to degrade to the stub.
func (f *Flags) InferenceBackend(ctx context.Context, def string) string {
	if v := strings.ToLower(strings.TrimSpace(f.stringFlag(ctx, keyInferenceBackend, def))); v != "" {
		return v
	}
	return def
}

// InferenceModel resolves inference.model. A blank
// value falls back to def.
func (f *Flags) InferenceModel(ctx context.Context, def string) string {
	if v := strings.TrimSpace(f.stringFlag(ctx, keyInferenceModel, def)); v != "" {
		return v
	}
	return def
}

// InferenceEffort resolves inference.effort. A blank
// value falls back to def; the inference client validates the level.
func (f *Flags) InferenceEffort(ctx context.Context, def string) string {
	if v := strings.TrimSpace(f.stringFlag(ctx, keyInferenceEffort, def)); v != "" {
		return v
	}
	return def
}

// InferenceTimeout resolves inference.timeout_seconds (int seconds).
func (f *Flags) InferenceTimeout(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyInferenceTimeoutSeconds, def)
}

// EffectWindow resolves effect.window_seconds (a
// float of seconds). A non-positive value falls back to def.
func (f *Flags) EffectWindow(ctx context.Context, def time.Duration) time.Duration {
	return f.durationFlag(ctx, keyEffectWindowSeconds, def.Seconds())
}

// RewatchInterval resolves promotion.rewatch_interval_seconds.
func (f *Flags) RewatchInterval(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyRewatchIntervalSeconds, def)
}

// RewatchTimeout resolves promotion.rewatch_timeout_seconds.
func (f *Flags) RewatchTimeout(ctx context.Context, def time.Duration) time.Duration {
	return f.secondsFlag(ctx, keyRewatchTimeoutSeconds, def)
}

// RevertCooldown resolves promotion.revert_cooldown_seconds.
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

// probeDecisions resolves decision.probe_decisions (false while flagd cannot resolve it).
// Read per Evaluate (per decision cycle), so a flip changes the NEXT cycle. The
// probe only narrows the agent (noop only), so it is tuning and never fails closed.
func (f *Flags) probeDecisions(ctx context.Context) bool {
	return f.boolFlag(ctx, keyProbeDecisions, false)
}
