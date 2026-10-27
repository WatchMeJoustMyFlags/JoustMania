package flags

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func defaultDecisionBackend() DecisionBackend {
	timeout := DefaultDecisionTimeoutMs * time.Millisecond
	return DecisionBackend{
		Backend:                DefaultDecisionBackend,
		ActConfidenceThreshold: DefaultDecisionActConfidenceThreshold,
		CaptureContent:         DefaultDecisionCaptureContent,
		MinInterval:            DefaultDecisionMinIntervalMs * time.Millisecond,
		MinGapAfterAnswer:      DefaultDecisionMinGapAfterAnswerMs * time.Millisecond,
		MaxStateAge:            DefaultDecisionMaxStateAgeMs * time.Millisecond,
		Claude: RemoteBackendConfig{BaseURL: DefaultDecisionClaudeBaseURL, Model: DefaultDecisionClaudeModel,
			Timeout: DefaultDecisionClaudeTimeoutMs * time.Millisecond},
		OpenAI: RemoteBackendConfig{BaseURL: DefaultDecisionOpenAIBaseURL, Timeout: timeout,
			ReasoningEffort: DefaultDecisionOpenAIReasoningEffort},
		Jev: RemoteBackendConfig{BaseURL: DefaultDecisionJevBaseURL, Model: DefaultDecisionJevModel, Timeout: timeout,
			ActConfidenceThreshold: DefaultDecisionJevActConfidenceThreshold, HasActConfidenceThreshold: true},
	}
}

func TestDecisionBackend_DefaultsWhenMissing(t *testing.T) {
	got := New(stubEvaluator{}, nil).Evaluate(context.Background()).Decision
	if got != defaultDecisionBackend() {
		t.Fatalf("Decision = %+v, want defaults %+v", got, defaultDecisionBackend())
	}
	if !got.IsRules() {
		t.Fatal("default backend must be rules")
	}
}

// TestDecisionBackend_DefaultTimeoutsPerBackend pins the NFR-010 default
// remote-call timeouts as literal values (the other tests compare against the
// constants, so a changed constant would pass them): Claude 5000 ms, OpenAI and
// Jev 2500 ms. TestDecisionBackend_FlagFilesMatchDefaults holds the flag files
// to the same values.
// NFR: NFR-010
// UC: UC-040 main
func TestDecisionBackend_DefaultTimeoutsPerBackend(t *testing.T) {
	got := New(stubEvaluator{}, nil).Evaluate(context.Background()).Decision
	for name, c := range map[string]struct{ got, want time.Duration }{
		"claude": {got.Claude.Timeout, 5000 * time.Millisecond},
		"openai": {got.OpenAI.Timeout, 2500 * time.Millisecond},
		"jev":    {got.Jev.Timeout, 2500 * time.Millisecond},
	} {
		t.Run("NFR-010 "+name+" default timeout", func(t *testing.T) {
			if c.got != c.want {
				t.Fatalf("decision.%s.timeout_ms default = %v, want %v", name, c.got, c.want)
			}
		})
	}
}

// UC: UC-044 main
func TestDecisionBackend_FlagdShape(t *testing.T) {
	stub := stubEvaluator{
		booleans: map[string]bool{keyDecisionCaptureContent: true},
		strings: map[string]string{
			keyDecisionBackend:               " Claude ",
			keyDecisionClaudeModel:           "claude-sonnet-5-5",
			keyDecisionClaudeBaseURL:         "http://127.0.0.1:1",
			keyDecisionOpenAIBaseURL:         "https://example.test/v1",
			keyDecisionOpenAIModel:           "gpt-x",
			keyDecisionOpenAIReasoningEffort: " Minimal ",
			keyDecisionJevBaseURL:            "https://jev.test",
			keyDecisionJevModel:              "jev-1",
		},
		ints: map[string]int64{
			keyDecisionClaudeTimeoutMs:     1500,
			keyDecisionOpenAITimeoutMs:     5000,
			keyDecisionJevTimeoutMs:        2000,
			keyDecisionMinIntervalMs:       1500,
			keyDecisionMinGapAfterAnswerMs: 3000,
			keyDecisionMaxStateAgeMs:       6000,
		},
		floats: map[string]float64{keyDecisionActConfidenceThreshold: 0.95, keyDecisionJevActConfidenceThreshold: 0.95},
	}
	got := New(stub, nil).DecisionBackend(context.Background())
	want := DecisionBackend{
		Backend:                DecisionBackendClaude,
		ActConfidenceThreshold: 0.95,
		CaptureContent:         true,
		MinInterval:            1500 * time.Millisecond,
		MinGapAfterAnswer:      3 * time.Second,
		MaxStateAge:            6 * time.Second,
		Claude:                 RemoteBackendConfig{BaseURL: "http://127.0.0.1:1", Model: "claude-sonnet-5-5", Timeout: 1500 * time.Millisecond},
		OpenAI: RemoteBackendConfig{BaseURL: "https://example.test/v1", Model: "gpt-x", Timeout: 5 * time.Second,
			ReasoningEffort: "minimal"},
		Jev: RemoteBackendConfig{BaseURL: "https://jev.test", Model: "jev-1", Timeout: 2 * time.Second,
			ActConfidenceThreshold: 0.95, HasActConfidenceThreshold: true},
	}
	if got != want {
		t.Fatalf("DecisionBackend = %+v, want %+v", got, want)
	}
	if got.IsRules() {
		t.Fatal("claude is not rules")
	}
	if cfg, ok := got.Remote(DecisionBackendOpenAI); !ok || cfg.Model != "gpt-x" {
		t.Fatalf("Remote(openai) = %+v %v", cfg, ok)
	}
	if _, ok := got.Remote(DecisionBackendRules); ok {
		t.Fatal("rules is not a remote backend")
	}
}

// TestDecisionBackend_ClaudeBaseURLReadPerCall: decision.claude.base_url is read
// on every DecisionBackend call (use time, not startup), so a flag flip applies
// to the next cycle without a restart (#1900).
// UC: UC-044 A3
func TestDecisionBackend_ClaudeBaseURLReadPerCall(t *testing.T) {
	stub := stubEvaluator{strings: map[string]string{keyDecisionClaudeBaseURL: "http://127.0.0.1:1"}}
	f := New(stub, nil)
	if got := f.DecisionBackend(context.Background()).Claude.BaseURL; got != "http://127.0.0.1:1" {
		t.Fatalf("first read = %q", got)
	}
	stub.strings[keyDecisionClaudeBaseURL] = "https://proxy.example.test"
	if got := f.DecisionBackend(context.Background()).Claude.BaseURL; got != "https://proxy.example.test" {
		t.Fatalf("second read = %q, want the flipped value", got)
	}
}

func TestDecisionBackend_BadValuesFallBack(t *testing.T) {
	stub := stubEvaluator{
		strings: map[string]string{keyDecisionBackend: "   ", keyDecisionOpenAIReasoningEffort: " "},
		ints: map[string]int64{keyDecisionClaudeTimeoutMs: 0, keyDecisionOpenAITimeoutMs: -5, keyDecisionMinIntervalMs: -1,
			keyDecisionMinGapAfterAnswerMs: -1, keyDecisionMaxStateAgeMs: -1},
		floats: map[string]float64{keyDecisionActConfidenceThreshold: 1.5},
	}
	got := New(stub, nil).DecisionBackend(context.Background())
	if got != defaultDecisionBackend() {
		t.Fatalf("Decision = %+v, want defaults %+v", got, defaultDecisionBackend())
	}
}

// TestDecisionBackend_JevThreshold covers decision.jev.act_confidence_threshold
// (#1287): its own value applies to jev only, "inherit" and out-of-range values
// fall back to the global flag, and claude/openai/rules always use the global.
// Spec: flags.md (decision.jev.act_confidence_threshold), decide-contract.md §5, FR-047.
// UC: UC-044 A3
func TestDecisionBackend_JevThreshold(t *testing.T) {
	cases := []struct {
		name       string
		jev        float64
		wantJev    float64
		wantSource string
	}{
		{"own gate", 0.6, 0.6, DecisionBackendJev},
		{"own zero", 0, 0, DecisionBackendJev},
		{"own one", 1, 1, DecisionBackendJev},
		{"inherit sentinel", ActConfidenceThresholdInherit, 0.95, ActThresholdSourceGlobal},
		{"above one inherits", 1.5, 0.95, ActThresholdSourceGlobal},
		{"negative inherits", -0.5, 0.95, ActThresholdSourceGlobal},
		{"NaN inherits", math.NaN(), 0.95, ActThresholdSourceGlobal},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stub := stubEvaluator{floats: map[string]float64{
				keyDecisionActConfidenceThreshold: 0.95, keyDecisionJevActConfidenceThreshold: tc.jev}}
			d := New(stub, nil).DecisionBackend(context.Background())
			if v, src := d.ActThreshold(DecisionBackendJev); v != tc.wantJev || src != tc.wantSource {
				t.Fatalf("ActThreshold(jev) = %v,%q, want %v,%q", v, src, tc.wantJev, tc.wantSource)
			}
			for _, b := range []string{DecisionBackendClaude, DecisionBackendOpenAI, DecisionBackendRules, "bogus"} {
				if v, src := d.ActThreshold(b); v != 0.95 || src != ActThresholdSourceGlobal {
					t.Fatalf("ActThreshold(%s) = %v,%q, want the global 0.95", b, v, src)
				}
			}
		})
	}
}

func TestDecisionBackend_JevThresholdDefaultActsAtGate(t *testing.T) {
	d := New(stubEvaluator{}, nil).DecisionBackend(context.Background())
	if v, src := d.ActThreshold(DecisionBackendJev); v != 0.6 || src != DecisionBackendJev {
		t.Fatalf("default ActThreshold(jev) = %v,%q, want 0.6 from jev", v, src)
	}
	if v, src := d.ActThreshold(DecisionBackendClaude); v != 0.8 || src != ActThresholdSourceGlobal {
		t.Fatalf("default ActThreshold(claude) = %v,%q, want the global 0.8", v, src)
	}
	if (DecisionBackend{}).Jev.HasActConfidenceThreshold {
		t.Fatal("a zero-value snapshot must inherit the global threshold")
	}
}

func TestDecisionBackend_DefaultsOnError(t *testing.T) {
	boom := errors.New("flagd down")
	stub := stubEvaluator{errs: map[string]error{}}
	for _, k := range []string{
		keyDecisionBackend, keyDecisionActConfidenceThreshold, keyDecisionCaptureContent,
		keyDecisionClaudeModel, keyDecisionClaudeTimeoutMs, keyDecisionClaudeBaseURL, keyDecisionOpenAIBaseURL,
		keyDecisionOpenAIModel, keyDecisionOpenAITimeoutMs, keyDecisionJevBaseURL,
		keyDecisionJevModel, keyDecisionJevTimeoutMs, keyDecisionMinIntervalMs,
		keyDecisionOpenAIReasoningEffort, keyDecisionJevActConfidenceThreshold,
	} {
		stub.errs[k] = boom
	}
	if got := New(stub, nil).DecisionBackend(context.Background()); got != defaultDecisionBackend() {
		t.Fatalf("Decision = %+v, want defaults", got)
	}
}

func TestDecisionBackend_ReasoningEffortOmit(t *testing.T) {
	stub := stubEvaluator{strings: map[string]string{keyDecisionOpenAIReasoningEffort: " OMIT "}}
	if got := New(stub, nil).DecisionBackend(context.Background()).OpenAI.ReasoningEffort; got != "omit" {
		t.Fatalf("ReasoningEffort = %q, want omit", got)
	}
}

func TestDecisionBackend_ZeroValueIsRules(t *testing.T) {
	if !(DecisionBackend{}).IsRules() {
		t.Fatal("a zero-value DecisionBackend (hand-built snapshot) must count as rules")
	}
}

// TestDecisionBackend_FlagFilesMatchDefaults keeps services/flagd/agent.json and
// ci/agent.json in sync with the Go defaults (the fail-safe values when flagd is
// unreachable). capture_content is the one intended difference: on in ci/.
func TestDecisionBackend_FlagFilesMatchDefaults(t *testing.T) {
	want := map[string]any{
		keyDecisionBackend:                   DefaultDecisionBackend,
		keyDecisionActConfidenceThreshold:    DefaultDecisionActConfidenceThreshold,
		keyDecisionClaudeModel:               DefaultDecisionClaudeModel,
		keyDecisionClaudeTimeoutMs:           float64(DefaultDecisionClaudeTimeoutMs),
		keyDecisionClaudeBaseURL:             DefaultDecisionClaudeBaseURL,
		keyDecisionOpenAIBaseURL:             DefaultDecisionOpenAIBaseURL,
		keyDecisionOpenAIModel:               DefaultDecisionOpenAIModel,
		keyDecisionOpenAITimeoutMs:           float64(DefaultDecisionTimeoutMs),
		keyDecisionOpenAIReasoningEffort:     DefaultDecisionOpenAIReasoningEffort,
		keyDecisionJevBaseURL:                DefaultDecisionJevBaseURL,
		keyDecisionJevModel:                  DefaultDecisionJevModel,
		keyDecisionJevTimeoutMs:              float64(DefaultDecisionTimeoutMs),
		keyDecisionMinIntervalMs:             float64(DefaultDecisionMinIntervalMs),
		keyDecisionJevActConfidenceThreshold: DefaultDecisionJevActConfidenceThreshold,
	}
	for path, capture := range map[string]bool{
		filepath.Join("..", "..", "flagd", "agent.json"):       DefaultDecisionCaptureContent,
		filepath.Join("..", "..", "flagd", "ci", "agent.json"): true,
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
		expect := map[string]any{keyDecisionCaptureContent: capture}
		for k, v := range want {
			expect[k] = v
		}
		for key, w := range expect {
			f, ok := doc.Flags[key]
			if !ok {
				t.Errorf("%s: flag %s missing", path, key)
				continue
			}
			if got := f.Variants[f.DefaultVariant]; got != w {
				t.Errorf("%s: %s default = %v, want %v", path, key, got, w)
			}
		}
	}
}

// TestDecisionBackend_JevBlackholeVariant pins the talk's degrade-beat variant
// (#743): decision.jev.base_url ships a non-default "blackhole" variant in both
// flag files that points at TEST-NET-1 (RFC 5737, never routed) over https, so
// the TLS handshake fails before any request header (the bearer key) is sent.
// The default stays DefaultDecisionJevBaseURL.
// UC: UC-070 main
func TestDecisionBackend_JevBlackholeVariant(t *testing.T) {
	for _, path := range []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
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
		f := doc.Flags[keyDecisionJevBaseURL]
		if got := f.Variants["blackhole"]; got != "https://192.0.2.1/v1" {
			t.Errorf("%s: %s blackhole = %v, want https://192.0.2.1/v1", path, keyDecisionJevBaseURL, got)
		}
		if f.DefaultVariant == "blackhole" {
			t.Errorf("%s: %s must not default to blackhole", path, keyDecisionJevBaseURL)
		}
		if got := f.Variants[f.DefaultVariant]; got != DefaultDecisionJevBaseURL {
			t.Errorf("%s: %s default = %v, want %s", path, keyDecisionJevBaseURL, got, DefaultDecisionJevBaseURL)
		}
	}
}

// TestDecisionBackend_StageBudgetVariant pins the presenter's live switch for the
// global remote-call budget (#1231): llm.max_requests_per_minute has a "stage"
// variant of 30 in both flag files, while the default stays
// DefaultLLMMaxRequestsPerMinute.
// UC: UC-070 A4
func TestDecisionBackend_StageBudgetVariant(t *testing.T) {
	for _, path := range []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
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
		// The jev threshold's "inherit" variant is the sentinel the getter maps to
		// the global flag (#1287).
		if got := doc.Flags[keyDecisionJevActConfidenceThreshold].Variants["inherit"]; got != ActConfidenceThresholdInherit {
			t.Errorf("%s: %s inherit = %v, want %v", path, keyDecisionJevActConfidenceThreshold, got, ActConfidenceThresholdInherit)
		}
		f := doc.Flags[keyLLMMaxRequestsPerMin]
		if got := f.Variants["stage"]; got != float64(30) {
			t.Errorf("%s: %s stage = %v, want 30", path, keyLLMMaxRequestsPerMin, got)
		}
		if got := f.Variants[f.DefaultVariant]; got != float64(DefaultLLMMaxRequestsPerMinute) {
			t.Errorf("%s: %s default = %v, want %d", path, keyLLMMaxRequestsPerMin, got, DefaultLLMMaxRequestsPerMinute)
		}
	}
}

// UC: UC-040 main
// Step 3 defaults: 3 s between remote calls of a game, 6 calls a minute, real games only.
func TestDecisionGate_Step3Defaults(t *testing.T) {
	snap := New(stubEvaluator{}, nil).Evaluate(context.Background())
	if snap.Decision.MinInterval != 3*time.Second {
		t.Errorf("decision.min_interval_ms default = %v, want 3s", snap.Decision.MinInterval)
	}
	if snap.LLMGate.MaxRequestsPerMinute != 6 {
		t.Errorf("llm.max_requests_per_minute default = %d, want 6", snap.LLMGate.MaxRequestsPerMinute)
	}
	if len(snap.LLMGate.EligibleGameKinds) != 1 || snap.LLMGate.EligibleGameKinds[0] != "real" {
		t.Errorf("llm.eligible_game_kinds default = %v, want [real] (real_only)", snap.LLMGate.EligibleGameKinds)
	}
}

// TestDecisionBackend_CadenceFlagDefaults pins the #2123 cadence flags: both flag
// files ship decision.min_gap_after_answer_ms = 1000 and
// decision.max_state_age_ms = 10000 as their defaults, matching the compiled
// fallbacks, and the stage baseline (scripts/talk-baseline.json) runs claude with
// the "patient" timeout (8000 ms), which the state-age bound stays above.
// UC: UC-040 A4
// UC: UC-070 main
func TestDecisionBackend_CadenceFlagDefaults(t *testing.T) {
	type flagDoc struct {
		Flags map[string]struct {
			Variants       map[string]any `json:"variants"`
			DefaultVariant string         `json:"defaultVariant"`
		} `json:"flags"`
	}
	for _, path := range []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
	} {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var doc flagDoc
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		for key, want := range map[string]int{
			keyDecisionMinGapAfterAnswerMs: DefaultDecisionMinGapAfterAnswerMs,
			keyDecisionMaxStateAgeMs:       DefaultDecisionMaxStateAgeMs,
		} {
			f, ok := doc.Flags[key]
			if !ok {
				t.Errorf("%s: %s missing", path, key)
				continue
			}
			if got := f.Variants[f.DefaultVariant]; got != float64(want) {
				t.Errorf("%s: %s default = %v, want %d", path, key, got, want)
			}
			if got := f.Variants["off"]; got != float64(0) {
				t.Errorf("%s: %s off = %v, want 0", path, key, got)
			}
		}
		patient := doc.Flags[keyDecisionClaudeTimeoutMs].Variants["patient"]
		if patient != float64(8000) {
			t.Errorf("%s: %s patient = %v, want 8000", path, keyDecisionClaudeTimeoutMs, patient)
		}
		if patient.(float64) >= float64(DefaultDecisionMaxStateAgeMs) {
			t.Errorf("%s: patient timeout %v must stay below the default state-age bound %d", path, patient, DefaultDecisionMaxStateAgeMs)
		}
	}
	snap := New(stubEvaluator{}, nil).Evaluate(context.Background())
	if snap.Decision.MinGapAfterAnswer != time.Second || snap.Decision.MaxStateAge != 10*time.Second {
		t.Errorf("compiled defaults gap=%v age=%v, want 1s and 10s", snap.Decision.MinGapAfterAnswer, snap.Decision.MaxStateAge)
	}

	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "scripts", "talk-baseline.json"))
	if err != nil {
		t.Fatal(err)
	}
	var baseline struct {
		Checked map[string]string `json:"checked"`
	}
	if err := json.Unmarshal(raw, &baseline); err != nil {
		t.Fatal(err)
	}
	if got := baseline.Checked[keyDecisionClaudeTimeoutMs]; got != "patient" {
		t.Errorf("talk baseline %s = %q, want patient (#2123)", keyDecisionClaudeTimeoutMs, got)
	}
	// On stage the 1 s after-answer gap dominates: the fire-to-fire floor is fast (1500 ms).
	if got := baseline.Checked[keyDecisionMinIntervalMs]; got != "fast" {
		t.Errorf("talk baseline %s = %q, want fast (#2123)", keyDecisionMinIntervalMs, got)
	}
}
