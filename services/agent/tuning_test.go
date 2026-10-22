package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/llm"
	"github.com/joustmania/agent/promote"
)

// tuning_test.go covers the #1788 agent tuning wiring (FR-150) through the real
// flags.Flags over a settable evaluator: the flag wins; the compiled default applies
// while the flag cannot be resolved (flagd down; the env fallbacks were removed,
// #1907); and every value is read at use time, so a flag change mid-run reaches the
// next use.

// tuningEvaluator is a flags.Evaluator whose values and reachability tests change
// at runtime. down = every read fails (flagd unreachable / no first sync).
type tuningEvaluator struct {
	mu      sync.Mutex
	down    bool
	strings map[string]string
	ints    map[string]int64
	floats  map[string]float64
}

var errFlagdDown = errors.New("PROVIDER_NOT_READY")

func newTuningEvaluator() *tuningEvaluator {
	return &tuningEvaluator{strings: map[string]string{}, ints: map[string]int64{}, floats: map[string]float64{}}
}

func (e *tuningEvaluator) setDown(down bool) { e.mu.Lock(); e.down = down; e.mu.Unlock() }
func (e *tuningEvaluator) setString(k, v string) {
	e.mu.Lock()
	e.strings[k] = v
	e.mu.Unlock()
}
func (e *tuningEvaluator) setInt(k string, v int64) { e.mu.Lock(); e.ints[k] = v; e.mu.Unlock() }
func (e *tuningEvaluator) setFloat(k string, v float64) {
	e.mu.Lock()
	e.floats[k] = v
	e.mu.Unlock()
}

func (e *tuningEvaluator) BooleanValue(_ context.Context, _ string, d bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	return d, nil
}
func (e *tuningEvaluator) StringValue(_ context.Context, k string, d string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.down {
		return d, errFlagdDown
	}
	if v, ok := e.strings[k]; ok {
		return v, nil
	}
	return d, errors.New("FLAG_NOT_FOUND")
}
func (e *tuningEvaluator) IntValue(_ context.Context, k string, d int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.down {
		return d, errFlagdDown
	}
	if v, ok := e.ints[k]; ok {
		return v, nil
	}
	return d, errors.New("FLAG_NOT_FOUND")
}
func (e *tuningEvaluator) FloatValue(_ context.Context, k string, d float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.down {
		return d, errFlagdDown
	}
	if v, ok := e.floats[k]; ok {
		return v, nil
	}
	return d, errors.New("FLAG_NOT_FOUND")
}
func (e *tuningEvaluator) ObjectValue(_ context.Context, _ string, d any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	return d, nil
}

// TestTuning_FlagDefaultPerSetting: for every migrated setting, (a) the flag wins,
// (b) the compiled default applies while the flag cannot be resolved, (e) a flag
// change is seen on the next read.
func TestTuning_FlagDefaultPerSetting(t *testing.T) {
	type setting struct {
		name              string
		setFlag           func(e *tuningEvaluator, second bool)
		read              func(t *agentTuning) any
		flagVal, flag2Val any
		defWant           any
	}
	settings := []setting{
		{
			name: "inference.model",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setString("inference.model", map[bool]string{false: "flag-model", true: "flag-model-2"}[second])
			},
			read:    func(t *agentTuning) any { return t.Inference(context.Background()).Model },
			flagVal: "flag-model", flag2Val: "flag-model-2", defWant: inference.DefaultModel,
		},
		{
			name: "inference.effort",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setString("inference.effort", map[bool]string{false: "high", true: "max"}[second])
			},
			read:    func(t *agentTuning) any { return t.Inference(context.Background()).Effort },
			flagVal: "high", flag2Val: "max", defWant: inference.DefaultEffort,
		},
		{
			name: "inference.timeout_seconds",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setInt("inference.timeout_seconds", map[bool]int64{false: 30, true: 90}[second])
			},
			read:    func(t *agentTuning) any { return t.InferenceTimeout() },
			flagVal: 30 * time.Second, flag2Val: 90 * time.Second, defWant: inference.DefaultTimeout,
		},
		{
			name: "effect.window_seconds",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setFloat("effect.window_seconds", map[bool]float64{false: 10, true: 40}[second])
			},
			read:    func(t *agentTuning) any { return t.EffectWindow() },
			flagVal: 10 * time.Second, flag2Val: 40 * time.Second, defWant: decision.DefaultEffectWindow,
		},
		{
			name: "promotion.rewatch_interval_seconds",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setInt("promotion.rewatch_interval_seconds", map[bool]int64{false: 20, true: 30}[second])
			},
			read:    func(t *agentTuning) any { return t.RewatchInterval() },
			flagVal: 20 * time.Second, flag2Val: 30 * time.Second, defWant: promote.DefaultRewatchInterval,
		},
		{
			name: "promotion.rewatch_timeout_seconds",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setInt("promotion.rewatch_timeout_seconds", map[bool]int64{false: 1200, true: 1800}[second])
			},
			read:    func(t *agentTuning) any { return t.RewatchTimeout() },
			flagVal: 1200 * time.Second, flag2Val: 1800 * time.Second, defWant: promote.DefaultRewatchTimeout,
		},
		{
			name: "promotion.revert_cooldown_seconds",
			setFlag: func(e *tuningEvaluator, second bool) {
				e.setInt("promotion.revert_cooldown_seconds", map[bool]int64{false: 300, true: 30}[second])
			},
			read:    func(t *agentTuning) any { return t.RevertCooldown() },
			flagVal: 300 * time.Second, flag2Val: 30 * time.Second, defWant: promote.DefaultRevertCooldown,
		},
	}
	for _, s := range settings {
		t.Run(s.name, func(t *testing.T) {
			ev := newTuningEvaluator()
			s.setFlag(ev, false)
			tuning := newAgentTuning(flags.New(ev, quietLogger()), compiledTuningDefaults())

			if got := s.read(tuning); got != s.flagVal {
				t.Fatalf("(a) flag set: got %v, want the flag %v", got, s.flagVal)
			}
			s.setFlag(ev, true)
			if got := s.read(tuning); got != s.flag2Val {
				t.Fatalf("(e) flag changed mid-run: got %v, want %v on the next use", got, s.flag2Val)
			}
			ev.setDown(true)
			if got := s.read(tuning); got != s.defWant {
				t.Fatalf("(b) flagd unreachable: got %v, want the compiled default %v", got, s.defWant)
			}
			ev.setDown(false)
			if got := s.read(tuning); got != s.flag2Val {
				t.Fatalf("flagd back: got %v, want the flag %v again", got, s.flag2Val)
			}

			bare := newAgentTuning(nil, compiledTuningDefaults())
			if got := s.read(bare); got != s.defWant {
				t.Fatalf("(c) no flag source: got %v, want the compiled default %v", got, s.defWant)
			}
		})
	}
}

// TestTuning_InferenceClientReadsFlagPerCall drives the real Claude client built by
// selectInferenceBackend against a fake Messages API: the model sent on each call
// follows the inference.model flag at that moment, and the compiled default while
// the flag cannot be resolved (AGENT_INFERENCE_MODEL is ignored, #1907).
func TestTuning_InferenceClientReadsFlagPerCall(t *testing.T) {
	var mu sync.Mutex
	var models []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var req struct {
			Model string `json:"model"`
		}
		_ = json.Unmarshal(raw, &req)
		mu.Lock()
		models = append(models, req.Model)
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"id":"msg_1","type":"message","role":"assistant","model":"`+req.Model+
			`","stop_reason":"end_turn","stop_sequence":null,"content":[{"type":"text","text":"{}"}],`+
			`"usage":{"input_tokens":1,"output_tokens":1}}`)
	}))
	t.Cleanup(srv.Close)

	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", srv.URL)

	ev := newTuningEvaluator()
	ev.setString("inference.model", "flag-model")
	tuning := newAgentTuning(flags.New(ev, quietLogger()), compiledTuningDefaults())
	client, name := selectInferenceBackend(quietLogger(), tuning, "claude")
	if client == nil || name != inference.Name {
		t.Fatalf("backend = (%v, %q), want the claude client", client != nil, name)
	}

	infer := func() {
		t.Helper()
		if _, err := client.Infer(context.Background(), llm.Prompt{User: "u"}); err != nil {
			t.Fatalf("Infer: %v", err)
		}
	}
	infer()
	ev.setString("inference.model", "flag-model-2")
	infer()
	ev.setDown(true)
	infer()

	mu.Lock()
	defer mu.Unlock()
	want := []string{"flag-model", "flag-model-2", inference.DefaultModel}
	if len(models) != len(want) {
		t.Fatalf("requests = %v, want %v", models, want)
	}
	for i := range want {
		if models[i] != want[i] {
			t.Errorf("call %d model = %q, want %q", i+1, models[i], want[i])
		}
	}
}

// TestLLMBudgetCeiling: AGENT_LLM_MAX_REQUESTS_PER_MINUTE is the operator's ceiling
// (#1788); unset or invalid = no ceiling (0).
// NFR: NFR-011
func TestLLMBudgetCeiling(t *testing.T) {
	for _, tc := range []struct {
		val  string
		want int
	}{{"", 0}, {"20", 20}, {"0", 0}, {"-5", 0}, {"lots", 0}} {
		t.Setenv("AGENT_LLM_MAX_REQUESTS_PER_MINUTE", tc.val)
		if got := llmBudgetCeiling(); got != tc.want {
			t.Errorf("llmBudgetCeiling(%q) = %d, want %d", tc.val, got, tc.want)
		}
	}
}

// TestTuningDefaults_IgnoreEnv: the removed env vars (#1907) have no effect. With every
// one set to a non-default value, the compiled defaults are unchanged, a text backend
// whose flag cannot be resolved is the stub, and a Claude client built without a
// flag-resolved tuning uses the inference constants.
// UC: UC-042 BR-003
func TestTuningDefaults_IgnoreEnv(t *testing.T) {
	want := compiledTuningDefaults()
	for k, v := range map[string]string{
		"AGENT_INFERENCE_BACKEND":                   "claude",
		"AGENT_INFERENCE_MODEL":                     "env-model",
		"AGENT_INFERENCE_EFFORT":                    "max",
		"AGENT_INFERENCE_TIMEOUT_SECONDS":           "7",
		"AGENT_EFFECT_WINDOW_SECONDS":               "3.5",
		"AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS": "5",
		"AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS":  "77",
		"AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS":  "9",
		"AGENT_PROBE_DECISIONS":                     "true",
		"ANTHROPIC_API_KEY":                         "sk-test",
	} {
		t.Setenv(k, v)
	}
	if got := compiledTuningDefaults(); got != want {
		t.Fatalf("compiledTuningDefaults changed with env set: %+v, want %+v", got, want)
	}
	if want.InferenceModel != inference.DefaultModel || want.InferenceEffort != inference.DefaultEffort ||
		want.InferenceTimeout != inference.DefaultTimeout || want.EffectWindow != decision.DefaultEffectWindow ||
		want.RewatchInterval != promote.DefaultRewatchInterval || want.RewatchTimeout != promote.DefaultRewatchTimeout ||
		want.RevertCooldown != promote.DefaultRevertCooldown {
		t.Fatalf("compiledTuningDefaults = %+v, want the package constants", want)
	}
	down := newTuningEvaluator()
	down.setDown(true)
	tb := newTextBackends(quietLogger(), nil, flags.New(down, quietLogger()))
	if client, name := tb.Select(context.Background()); client != nil || name != "stub" {
		t.Fatalf("Select with flagd down = (%v, %q), want (nil, stub) despite AGENT_INFERENCE_BACKEND=claude", client != nil, name)
	}
	if got := flags.New(down, quietLogger()).Evaluate(context.Background()).ProbeDecisions; got {
		t.Fatal("probe decisions on with flagd down: AGENT_PROBE_DECISIONS must be ignored")
	}
}

// TestCompiledDefaultsMatchFlagDefaults pins every compiled fallback to the default
// variant of its flag in services/flagd/agent.json and ci/agent.json: with the env
// fallbacks gone (#1907), the compiled value is what runs while flagd cannot answer,
// so it must equal what flagd serves once it can. Later #1907 PRs add their keys here.
// UC: UC-042 Pre-2
func TestCompiledDefaultsMatchFlagDefaults(t *testing.T) {
	d := compiledTuningDefaults()
	want := map[string]any{
		"inference.backend":                  envFallback,
		"inference.model":                    d.InferenceModel,
		"inference.effort":                   d.InferenceEffort,
		"inference.timeout_seconds":          d.InferenceTimeout.Seconds(),
		"effect.window_seconds":              d.EffectWindow.Seconds(),
		"promotion.rewatch_interval_seconds": d.RewatchInterval.Seconds(),
		"promotion.rewatch_timeout_seconds":  d.RewatchTimeout.Seconds(),
		"promotion.revert_cooldown_seconds":  d.RevertCooldown.Seconds(),
		"decision.probe_decisions":           false, // main.go SetProbeDefault(false)
	}
	for _, path := range []string{
		filepath.Join("..", "flagd", "agent.json"),
		filepath.Join("..", "flagd", "ci", "agent.json"),
	} {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var doc struct {
			Flags map[string]struct {
				Variants       map[string]any `json:"variants"`
				DefaultVariant string         `json:"defaultVariant"`
			} `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		for key, w := range want {
			f, ok := doc.Flags[key]
			if !ok {
				t.Errorf("%s: flag %q missing", path, key)
				continue
			}
			if got := f.Variants[f.DefaultVariant]; got != w {
				t.Errorf("%s: %s default variant = %v, compiled default = %v", path, key, got, w)
			}
		}
	}
}
