package flags

import (
	"context"
	"encoding/json"
	"errors"
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
		Claude:                 RemoteBackendConfig{Model: DefaultDecisionClaudeModel, Timeout: timeout},
		OpenAI: RemoteBackendConfig{BaseURL: DefaultDecisionOpenAIBaseURL, Timeout: timeout,
			ReasoningEffort: DefaultDecisionOpenAIReasoningEffort},
		Jev: RemoteBackendConfig{Timeout: timeout},
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

func TestDecisionBackend_FlagdShape(t *testing.T) {
	stub := stubEvaluator{
		booleans: map[string]bool{keyDecisionCaptureContent: true},
		strings: map[string]string{
			keyDecisionBackend:               " Claude ",
			keyDecisionClaudeModel:           "claude-sonnet-5-5",
			keyDecisionOpenAIBaseURL:         "https://example.test/v1",
			keyDecisionOpenAIModel:           "gpt-x",
			keyDecisionOpenAIReasoningEffort: " Minimal ",
			keyDecisionJevBaseURL:            "https://jev.test",
			keyDecisionJevModel:              "jev-1",
		},
		ints: map[string]int64{
			keyDecisionClaudeTimeoutMs: 1500,
			keyDecisionOpenAITimeoutMs: 5000,
			keyDecisionJevTimeoutMs:    2000,
			keyDecisionMinIntervalMs:   1500,
		},
		floats: map[string]float64{keyDecisionActConfidenceThreshold: 0.95},
	}
	got := New(stub, nil).DecisionBackend(context.Background())
	want := DecisionBackend{
		Backend:                DecisionBackendClaude,
		ActConfidenceThreshold: 0.95,
		CaptureContent:         true,
		MinInterval:            1500 * time.Millisecond,
		Claude:                 RemoteBackendConfig{Model: "claude-sonnet-5-5", Timeout: 1500 * time.Millisecond},
		OpenAI: RemoteBackendConfig{BaseURL: "https://example.test/v1", Model: "gpt-x", Timeout: 5 * time.Second,
			ReasoningEffort: "minimal"},
		Jev: RemoteBackendConfig{BaseURL: "https://jev.test", Model: "jev-1", Timeout: 2 * time.Second},
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

func TestDecisionBackend_BadValuesFallBack(t *testing.T) {
	stub := stubEvaluator{
		strings: map[string]string{keyDecisionBackend: "   ", keyDecisionOpenAIReasoningEffort: " "},
		ints:    map[string]int64{keyDecisionClaudeTimeoutMs: 0, keyDecisionOpenAITimeoutMs: -5, keyDecisionMinIntervalMs: -1},
		floats:  map[string]float64{keyDecisionActConfidenceThreshold: 1.5},
	}
	got := New(stub, nil).DecisionBackend(context.Background())
	if got != defaultDecisionBackend() {
		t.Fatalf("Decision = %+v, want defaults %+v", got, defaultDecisionBackend())
	}
}

func TestDecisionBackend_DefaultsOnError(t *testing.T) {
	boom := errors.New("flagd down")
	stub := stubEvaluator{errs: map[string]error{}}
	for _, k := range []string{
		keyDecisionBackend, keyDecisionActConfidenceThreshold, keyDecisionCaptureContent,
		keyDecisionClaudeModel, keyDecisionClaudeTimeoutMs, keyDecisionOpenAIBaseURL,
		keyDecisionOpenAIModel, keyDecisionOpenAITimeoutMs, keyDecisionJevBaseURL,
		keyDecisionJevModel, keyDecisionJevTimeoutMs, keyDecisionMinIntervalMs,
		keyDecisionOpenAIReasoningEffort,
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
		keyDecisionBackend:                DefaultDecisionBackend,
		keyDecisionActConfidenceThreshold: DefaultDecisionActConfidenceThreshold,
		keyDecisionClaudeModel:            DefaultDecisionClaudeModel,
		keyDecisionClaudeTimeoutMs:        float64(DefaultDecisionTimeoutMs),
		keyDecisionOpenAIBaseURL:          DefaultDecisionOpenAIBaseURL,
		keyDecisionOpenAIModel:            DefaultDecisionOpenAIModel,
		keyDecisionOpenAITimeoutMs:        float64(DefaultDecisionTimeoutMs),
		keyDecisionOpenAIReasoningEffort:  DefaultDecisionOpenAIReasoningEffort,
		keyDecisionJevBaseURL:             DefaultDecisionJevBaseURL,
		keyDecisionJevModel:               DefaultDecisionJevModel,
		keyDecisionJevTimeoutMs:           float64(DefaultDecisionTimeoutMs),
		keyDecisionMinIntervalMs:          float64(DefaultDecisionMinIntervalMs),
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
