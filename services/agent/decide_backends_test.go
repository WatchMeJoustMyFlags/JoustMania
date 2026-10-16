package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/backends/claude"
	"github.com/joustmania/agent/decide/backends/jev"
	"github.com/joustmania/agent/decide/backends/openai"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
)

// mapEvaluator is a flagd stand-in: string/int flags from maps, everything else
// (and any missing key) errors so the flags package applies its defaults, exactly
// as when a flag is undefined in flagd.
type mapEvaluator struct {
	strings map[string]string
	ints    map[string]int64
}

var errFlagMissing = errors.New("flag not found")

func (m mapEvaluator) BooleanValue(_ context.Context, _ string, d bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	return d, errFlagMissing
}

func (m mapEvaluator) StringValue(_ context.Context, k string, d string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	if v, ok := m.strings[k]; ok {
		return v, nil
	}
	return d, errFlagMissing
}

func (m mapEvaluator) IntValue(_ context.Context, k string, d int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	if v, ok := m.ints[k]; ok {
		return v, nil
	}
	return d, errFlagMissing
}

func (m mapEvaluator) FloatValue(_ context.Context, _ string, d float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	return d, errFlagMissing
}

func (m mapEvaluator) ObjectValue(_ context.Context, _ string, d any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	return d, errFlagMissing
}

// decisionFlags resolves the decision-backend flag group through the real flags
// getter (normalization + defaults) from the given flag values.
func decisionFlags(t *testing.T, strs map[string]string) flags.DecisionBackend {
	t.Helper()
	return flags.New(mapEvaluator{strings: strs}, nil).DecisionBackend(context.Background())
}

var remoteBackends = []string{flags.DecisionBackendClaude, flags.DecisionBackendOpenAI, flags.DecisionBackendJev}

// fullyConfigured sets the model / base URL flags every remote backend needs (the
// shipped defaults leave openai's model unset).
func fullyConfigured(backend string) map[string]string {
	return map[string]string{
		"decision.backend":                 backend,
		"decision.openai.model":            "gpt-5-mini",
		"decision.jev.base_url":            "https://jev.example.test",
		"decision.jev.model":               "jev-1",
		"decision.claude.model":            "claude-haiku-4-5-20251001",
		"decision.openai.reasoning_effort": "auto",
	}
}

var allKeys = decideBackendEnv{AnthropicAPIKey: "sk-ant-test", OpenAIAPIKey: "sk-test", JevAPIKey: "jev-test"}

// TestDecideRegistry_NoKeysFallsBackToRules: the default deployment (no API keys
// in the env) resolves every remote backend to backend_unavailable, so the loop
// decides with rules, even with models and base URLs configured.
func TestDecideRegistry_NoKeysFallsBackToRules(t *testing.T) {
	reg := newDecideRegistry(decideBackendEnv{}, newDecideHTTPClient())
	for _, name := range remoteBackends {
		for _, strs := range []map[string]string{
			{"decision.backend": name}, // shipped defaults
			fullyConfigured(name),
		} {
			b, reason := reg.Resolve(decisionFlags(t, strs))
			if b != nil || reason != decision.DecideFallbackUnavailable {
				t.Errorf("%s with no keys: Resolve = (%v, %q), want (nil, %q)", name, b, reason, decision.DecideFallbackUnavailable)
			}
		}
	}
}

// TestDecideRegistry_KeyWithoutConfigFallsBack: a key alone is not enough when the
// shipped defaults leave openai's model empty. claude and jev have a default
// model (and jev a default base URL), so their key alone makes them available:
// scripts/agent-backend.sh jev works with only JEV_API_KEY set (#1229).
func TestDecideRegistry_KeyWithoutConfigFallsBack(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	for name, wantAvailable := range map[string]bool{
		flags.DecisionBackendClaude: true,
		flags.DecisionBackendOpenAI: false,
		flags.DecisionBackendJev:    true,
	} {
		b, reason := reg.Resolve(decisionFlags(t, map[string]string{"decision.backend": name}))
		if got := b != nil; got != wantAvailable {
			t.Errorf("%s with key + shipped defaults: Resolve = (%v, %q), want available=%v", name, b, reason, wantAvailable)
		}
		if !wantAvailable && reason != decision.DecideFallbackUnavailable {
			t.Errorf("%s: reason = %q, want %q", name, reason, decision.DecideFallbackUnavailable)
		}
	}
}

// TestDecideRegistry_KeysResolveEachBackend: with keys and config every remote
// backend resolves to itself, and an unknown name is still unknown_backend.
func TestDecideRegistry_KeysResolveEachBackend(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	for _, name := range remoteBackends {
		b, reason := reg.Resolve(decisionFlags(t, fullyConfigured(name)))
		if b == nil || reason != "" {
			t.Fatalf("%s: Resolve = (%v, %q), want a backend", name, b, reason)
		}
		if b.Name() != name {
			t.Errorf("%s: resolved backend %q", name, b.Name())
		}
	}
	if b, reason := reg.Resolve(decisionFlags(t, fullyConfigured("gemini"))); b != nil || reason != decision.DecideFallbackUnknownBackend {
		t.Errorf("unknown backend: Resolve = (%v, %q), want unknown_backend", b, reason)
	}
}

type modeler interface {
	decide.Backend
	Model() string
}

// TestDecideRegistry_ModelFlagAppliesNextResolve: a model flag flip yields a
// backend with the new model on the very next Resolve (no restart), for every
// remote backend, including the cached claude one.
func TestDecideRegistry_ModelFlagAppliesNextResolve(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	modelKey := map[string]string{
		flags.DecisionBackendClaude: "decision.claude.model",
		flags.DecisionBackendOpenAI: "decision.openai.model",
		flags.DecisionBackendJev:    "decision.jev.model",
	}
	for _, name := range remoteBackends {
		strs := fullyConfigured(name)
		for _, model := range []string{"model-a", "model-b", "model-a"} {
			strs[modelKey[name]] = model
			b, reason := reg.Resolve(decisionFlags(t, strs))
			if b == nil {
				t.Fatalf("%s model %s: Resolve fell back (%q)", name, model, reason)
			}
			if got := b.(modeler).Model(); got != model {
				t.Errorf("%s: model = %q after flipping the flag to %q", name, got, model)
			}
		}
	}
}

// TestDecideRegistry_ClaudeBackendCached: the claude factory reuses one backend
// per config (claude.New builds an SDK client) and builds a new one when a flag
// changes; the cache stays bounded under flag churn.
func TestDecideRegistry_ClaudeBackendCached(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	cfg := decisionFlags(t, fullyConfigured(flags.DecisionBackendClaude))
	first, _ := reg.Resolve(cfg)
	again, _ := reg.Resolve(cfg)
	if first != again {
		t.Error("same claude config must reuse the cached backend")
	}
	cfg.Claude.Timeout = 5 * time.Second
	if other, _ := reg.Resolve(cfg); other == first {
		t.Error("a timeout flag change must build a new claude backend")
	}
	cfg.CaptureContent = true
	if other, _ := reg.Resolve(cfg); other == first {
		t.Error("a capture flag change must build a new claude backend")
	}

	cache := newClaudeCache(claudeCacheSize)
	for i := 0; i < 3*claudeCacheSize; i++ {
		cache.get(claude.Config{APIKey: "k", Model: fmt.Sprintf("m-%d", i)})
		if n := cache.size(); n > claudeCacheSize {
			t.Fatalf("cache grew to %d entries, bound %d", n, claudeCacheSize)
		}
	}
	c := claude.Config{APIKey: "k", Model: "steady"}
	if cache.get(c) != cache.get(c) {
		t.Error("cache miss for an unchanged config")
	}
}

// TestDecideRegistry_ClaudeCacheConcurrent: per-game loops resolve concurrently.
func TestDecideRegistry_ClaudeCacheConcurrent(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			strs := fullyConfigured(flags.DecisionBackendClaude)
			strs["decision.claude.model"] = fmt.Sprintf("m-%d", i%(2*claudeCacheSize))
			for j := 0; j < 50; j++ {
				if b, reason := reg.Resolve(decisionFlags(t, strs)); b == nil {
					t.Errorf("Resolve fell back: %q", reason)
					return
				}
			}
		}(i)
	}
	wg.Wait()
}

// TestDecideRegistry_OpenAIWiresKeyAndFlags drives the resolved openai backend
// against a fake endpoint: the env key is the Bearer token, and the model and
// reasoning_effort flags reach the wire (a flag flip applies on the next call).
func TestDecideRegistry_OpenAIWiresKeyAndFlags(t *testing.T) {
	type seen struct {
		auth string
		body map[string]any
	}
	var (
		mu    sync.Mutex
		calls []seen
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		mu.Lock()
		calls = append(calls, seen{auth: r.Header.Get("Authorization"), body: body})
		mu.Unlock()
		http.Error(w, `{"error":{"message":"nope"}}`, http.StatusUnauthorized)
	}))
	defer srv.Close()

	reg := newDecideRegistry(allKeys, srv.Client())
	strs := fullyConfigured(flags.DecisionBackendOpenAI)
	strs["decision.openai.base_url"] = srv.URL + "/v1"
	req := decide.Request{Questions: []decide.Question{{ID: decide.QuestionFFAIntervention}}}

	for i, step := range []struct{ model, effort, wantEffort string }{
		{"gpt-5-mini", "auto", "minimal"},
		{"gpt-6", "auto", "low"},
		{"gpt-6", "none", "none"},
	} {
		strs["decision.openai.model"] = step.model
		strs["decision.openai.reasoning_effort"] = step.effort
		b, reason := reg.Resolve(decisionFlags(t, strs))
		if b == nil {
			t.Fatalf("Resolve fell back: %q", reason)
		}
		if _, err := b.Decide(context.Background(), req); err == nil {
			t.Fatal("a 401 must be an error")
		}
		mu.Lock()
		got := calls[i]
		mu.Unlock()
		if got.auth != "Bearer "+allKeys.OpenAIAPIKey {
			t.Errorf("Authorization = %q, want the DECISION_OPENAI_API_KEY bearer", got.auth)
		}
		if got.body["model"] != step.model || got.body["reasoning_effort"] != step.wantEffort {
			t.Errorf("step %d: model=%v reasoning_effort=%v, want %s / %s",
				i, got.body["model"], got.body["reasoning_effort"], step.model, step.wantEffort)
		}
	}
}

// TestDecideBackendEnvFromOS reads (and trims) the three key variables.
func TestDecideBackendEnvFromOS(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", " a ")
	t.Setenv("DECISION_OPENAI_API_KEY", "o")
	t.Setenv("JEV_API_KEY", "")
	got := decideBackendEnvFromOS()
	if got != (decideBackendEnv{AnthropicAPIKey: "a", OpenAIAPIKey: "o"}) {
		t.Errorf("env = %+v", got)
	}
}

// compile-time: the concrete backends expose Model for the flip test.
var (
	_ modeler = (*claude.Backend)(nil)
	_ modeler = (*openai.Backend)(nil)
	_ modeler = (*jev.Backend)(nil)
)

// TestJevFlagDefaultsMatchBackend: the flag fail-safe defaults are the jev
// backend's own defaults, so a flagd outage changes nothing for jev.
func TestJevFlagDefaultsMatchBackend(t *testing.T) {
	if flags.DefaultDecisionJevBaseURL != jev.DefaultBaseURL || flags.DefaultDecisionJevModel != jev.DefaultModel {
		t.Errorf("flags %q/%q vs backend %q/%q", flags.DefaultDecisionJevBaseURL, flags.DefaultDecisionJevModel, jev.DefaultBaseURL, jev.DefaultModel)
	}
	b, _ := newDecideRegistry(decideBackendEnv{JevAPIKey: "jev-test"}, newDecideHTTPClient()).
		Resolve(decisionFlags(t, map[string]string{"decision.backend": flags.DecisionBackendJev}))
	if b == nil || b.(modeler).Model() != jev.DefaultModel {
		t.Fatalf("jev with key + shipped defaults = %v", b)
	}
}
