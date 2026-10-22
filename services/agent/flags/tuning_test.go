package flags

import (
	"context"
	"errors"
	"testing"
	"time"
)

// tuning_test.go covers the #1788 tuning getters (FR-150, C-035): the flag wins
// when it resolves; an evaluation error (flagd unreachable, flag missing, no first
// sync) or an unusable value (blank string, non-positive number) yields the
// caller's fallback, which main.go builds from the deprecated env var or the
// compiled default.

func TestTuning_FlagWinsOverFallback(t *testing.T) {
	ev := stubEvaluator{
		strings: map[string]string{keyInferenceModel: "claude-sonnet-5-5", keyInferenceEffort: "high"},
		ints: map[string]int64{
			keyInferenceTimeoutSeconds: 30, keyRewatchIntervalSeconds: 15,
			keyRewatchTimeoutSeconds: 900, keyRevertCooldownSeconds: 120,
		},
		floats: map[string]float64{keyEffectWindowSeconds: 12.5},
	}
	f := New(ev, nil)
	ctx := context.Background()
	fallback := 7 * time.Second
	checks := []struct {
		name      string
		got, want any
	}{
		{"inference.model", f.InferenceModel(ctx, "env-model"), "claude-sonnet-5-5"},
		{"inference.effort", f.InferenceEffort(ctx, "low"), "high"},
		{"inference.timeout_seconds", f.InferenceTimeout(ctx, fallback), 30 * time.Second},
		{"effect.window_seconds", f.EffectWindow(ctx, fallback), 12500 * time.Millisecond},
		{"promotion.rewatch_interval_seconds", f.RewatchInterval(ctx, fallback), 15 * time.Second},
		{"promotion.rewatch_timeout_seconds", f.RewatchTimeout(ctx, fallback), 900 * time.Second},
		{"promotion.revert_cooldown_seconds", f.RevertCooldown(ctx, fallback), 120 * time.Second},
	}
	for _, c := range checks {
		if c.got != c.want {
			t.Errorf("%s = %v, want the flag value %v", c.name, c.got, c.want)
		}
	}
}

func TestTuning_UnresolvableFlagUsesFallback(t *testing.T) {
	down := errors.New("PROVIDER_NOT_READY")
	errs := map[string]error{}
	for _, k := range []string{keyInferenceModel, keyInferenceEffort, keyInferenceTimeoutSeconds, keyEffectWindowSeconds,
		keyRewatchIntervalSeconds, keyRewatchTimeoutSeconds, keyRevertCooldownSeconds} {
		errs[k] = down
	}
	for name, ev := range map[string]stubEvaluator{
		"flagd unreachable": {
			strings: map[string]string{keyInferenceModel: "flag-model", keyInferenceEffort: "max"},
			ints:    map[string]int64{keyInferenceTimeoutSeconds: 30, keyRewatchIntervalSeconds: 15, keyRewatchTimeoutSeconds: 900, keyRevertCooldownSeconds: 120},
			floats:  map[string]float64{keyEffectWindowSeconds: 12.5},
			errs:    errs,
		},
		"flag missing": {},
		"unusable values": {
			strings: map[string]string{keyInferenceModel: "  ", keyInferenceEffort: ""},
			ints:    map[string]int64{keyInferenceTimeoutSeconds: 0, keyRewatchIntervalSeconds: -1, keyRewatchTimeoutSeconds: 0, keyRevertCooldownSeconds: -5},
			floats:  map[string]float64{keyEffectWindowSeconds: 0},
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := New(ev, nil)
			ctx := context.Background()
			fb := 7 * time.Second
			if got := f.InferenceModel(ctx, "env-model"); got != "env-model" {
				t.Errorf("inference.model = %q, want the fallback", got)
			}
			if got := f.InferenceEffort(ctx, "low"); got != "low" {
				t.Errorf("inference.effort = %q, want the fallback", got)
			}
			for key, got := range map[string]time.Duration{
				"inference.timeout_seconds":          f.InferenceTimeout(ctx, fb),
				"effect.window_seconds":              f.EffectWindow(ctx, fb),
				"promotion.rewatch_interval_seconds": f.RewatchInterval(ctx, fb),
				"promotion.rewatch_timeout_seconds":  f.RewatchTimeout(ctx, fb),
				"promotion.revert_cooldown_seconds":  f.RevertCooldown(ctx, fb),
			} {
				if got != fb {
					t.Errorf("%s = %v, want the fallback %v", key, got, fb)
				}
			}
		})
	}
}

func TestTuning_ReadAtUseTime(t *testing.T) {
	ev := stubEvaluator{strings: map[string]string{keyInferenceModel: "model-a"}, ints: map[string]int64{keyRevertCooldownSeconds: 60}}
	f := New(ev, nil)
	ctx := context.Background()
	if got := f.InferenceModel(ctx, "env"); got != "model-a" {
		t.Fatalf("first read = %q, want model-a", got)
	}
	ev.strings[keyInferenceModel] = "model-b" // flag changed mid-run
	ev.ints[keyRevertCooldownSeconds] = 300
	if got := f.InferenceModel(ctx, "env"); got != "model-b" {
		t.Fatalf("second read = %q, want model-b (no caching)", got)
	}
	if got := f.RevertCooldown(ctx, time.Second); got != 300*time.Second {
		t.Fatalf("cooldown = %v, want 300s (no caching)", got)
	}
}

// UC: UC-042 Pre-2
func TestInferenceBackend_FlagWinsThenFallback(t *testing.T) {
	ctx := context.Background()
	// The flag wins over the env-derived fallback, normalised (case, spaces).
	f := New(stubEvaluator{strings: map[string]string{keyInferenceBackend: " Claude "}}, nil)
	if got := f.InferenceBackend(ctx, "stub"); got != "claude" {
		t.Errorf("flag claude, env stub = %q, want claude (the flag always wins)", got)
	}
	f = New(stubEvaluator{strings: map[string]string{keyInferenceBackend: "stub"}}, nil)
	if got := f.InferenceBackend(ctx, "claude"); got != "stub" {
		t.Errorf("flag stub, env claude = %q, want stub (FR-150)", got)
	}
	// Unresolvable (flagd down / flag missing) -> the caller's fallback.
	down := New(stubEvaluator{errs: map[string]error{keyInferenceBackend: errors.New("PROVIDER_NOT_READY")}}, nil)
	if got := down.InferenceBackend(ctx, "claude"); got != "claude" {
		t.Errorf("flagd down = %q, want the env fallback claude", got)
	}
	// A blank flag value also falls back.
	blank := New(stubEvaluator{strings: map[string]string{keyInferenceBackend: " "}}, nil)
	if got := blank.InferenceBackend(ctx, "stub"); got != "stub" {
		t.Errorf("blank = %q, want fallback stub", got)
	}
}
