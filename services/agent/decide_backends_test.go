package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"go.opentelemetry.io/otel"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/backends/claude"
	"github.com/joustmania/agent/decide/backends/jev"
	"github.com/joustmania/agent/decide/backends/openai"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/keysafe"
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
// UC: UC-044 A1 BR-001
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
// UC: UC-044 A1
// UC: UC-070 A6
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
// UC: UC-044 Pre-1
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
// UC: UC-044 main
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
// changes; the cache stays bounded under flag churn. A capture_content flip
// (agent-backend.sh capture on) applies on the next resolve.
// UC: UC-044 A3
func TestDecideRegistry_ClaudeBackendCached(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	cfg := decisionFlags(t, fullyConfigured(flags.DecisionBackendClaude))
	first, _ := reg.Resolve(cfg)
	again, _ := reg.Resolve(cfg)
	if first != again {
		t.Error("same claude config must reuse the cached backend")
	}
	cfg.Claude.Timeout += time.Second
	if other, _ := reg.Resolve(cfg); other == first {
		t.Error("a timeout flag change must build a new claude backend")
	}
	cfg.CaptureContent = true
	if other, _ := reg.Resolve(cfg); other == first {
		t.Error("a capture flag change must build a new claude backend")
	}
	base, _ := reg.Resolve(decisionFlags(t, fullyConfigured(flags.DecisionBackendClaude)))
	moved := fullyConfigured(flags.DecisionBackendClaude)
	moved["decision.claude.base_url"] = "http://127.0.0.1:1"
	if other, _ := reg.Resolve(decisionFlags(t, moved)); other == nil || other == base {
		t.Error("a base_url flag change must build a new claude backend (#1900)")
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
// UC: UC-070 A6
// UC: UC-044 A3
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
// UC: UC-044 BR-001
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
// UC: UC-044 BR-002
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

// TestDecideRegistry_EmptyBaseURL: an empty decision.openai.base_url has no
// endpoint to call, so openai falls back to rules (backend_unavailable) even with
// a key and a model. Jev is the documented exception: an empty base URL means
// jev.DefaultBaseURL, so jev stays available.
// UC: UC-044 A1
func TestDecideRegistry_EmptyBaseURL(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())

	strs := fullyConfigured(flags.DecisionBackendOpenAI)
	strs["decision.openai.base_url"] = ""
	b, reason := reg.Resolve(decisionFlags(t, strs))
	if b != nil || reason != decision.DecideFallbackUnavailable {
		t.Errorf("openai with an empty base URL: Resolve = (%v, %q), want (nil, %q)", b, reason, decision.DecideFallbackUnavailable)
	}

	strs = fullyConfigured(flags.DecisionBackendJev)
	strs["decision.jev.base_url"] = ""
	if b, reason := reg.Resolve(decisionFlags(t, strs)); b == nil {
		t.Errorf("jev with an empty base URL must default it, got fallback %q", reason)
	}

	// Claude, like jev: an empty base URL means claude.DefaultBaseURL (#1900).
	strs = fullyConfigured(flags.DecisionBackendClaude)
	strs["decision.claude.base_url"] = ""
	if b, reason := reg.Resolve(decisionFlags(t, strs)); b == nil {
		t.Errorf("claude with an empty base URL must default it, got fallback %q", reason)
	}
}

// TestClaudeFlagDefaultsMatchBackend: the decision.claude.base_url default is the
// claude package default in Go and in both flag files, with NO /v1 (the SDK
// appends /v1/messages; a /v1 would post to /v1/v1/messages and 404 every call).
// The `fake` variant exists only in ci/: a prod overlay must not be able to
// select a dead loopback URL.
// UC: UC-044 BR-002
func TestClaudeFlagDefaultsMatchBackend(t *testing.T) {
	if flags.DefaultDecisionClaudeBaseURL != claude.DefaultBaseURL {
		t.Fatalf("flags default %q != claude.DefaultBaseURL %q", flags.DefaultDecisionClaudeBaseURL, claude.DefaultBaseURL)
	}
	if strings.HasSuffix(claude.DefaultBaseURL, "/v1") {
		t.Fatal("claude base URL must not end in /v1")
	}
	for _, f := range shippedFlagFiles(t) {
		if filepath.Base(f) != "agent.json" {
			continue
		}
		raw, err := os.ReadFile(f)
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
			t.Fatalf("parse %s: %v", f, err)
		}
		fl, ok := doc.Flags["decision.claude.base_url"]
		if !ok {
			t.Fatalf("%s: decision.claude.base_url missing", f)
		}
		if got, _ := fl.Variants[fl.DefaultVariant].(string); got != claude.DefaultBaseURL {
			t.Errorf("%s: default = %q, want %q", f, got, claude.DefaultBaseURL)
		}
		fakeAny, hasFake := fl.Variants["fake"]
		fake, _ := fakeAny.(string)
		isCI := strings.Contains(filepath.ToSlash(f), "/ci/")
		if hasFake != isCI {
			t.Errorf("%s: fake variant present=%v, want %v (ci/ only)", f, hasFake, isCI)
		}
		if hasFake && fake != "http://127.0.0.1:1" {
			t.Errorf("%s: fake = %q, want http://127.0.0.1:1 (no /v1)", f, fake)
		}
		if !isCI && len(fl.Variants) != 1 {
			t.Errorf("%s: prod variants = %v, want only default", f, fl.Variants)
		}
	}
}

// TestDecideRegistry_KeysFixedAtStartup: the keys are read once, so an env change
// after the registry exists does not make an unavailable backend available (a new
// key needs a restart) and does not swap the key of an available one.
// UC: UC-044 BR-001
func TestDecideRegistry_KeysFixedAtStartup(t *testing.T) {
	for _, name := range []string{"ANTHROPIC_API_KEY", "DECISION_OPENAI_API_KEY", "JEV_API_KEY"} {
		t.Setenv(name, "")
	}
	t.Setenv("JEV_API_KEY", "jev-at-startup")
	reg := newDecideRegistry(decideBackendEnvFromOS(), newDecideHTTPClient())

	// A key that appears after startup changes nothing.
	t.Setenv("ANTHROPIC_API_KEY", "sk-ant-late")
	t.Setenv("DECISION_OPENAI_API_KEY", "sk-late")
	for _, name := range []string{flags.DecisionBackendClaude, flags.DecisionBackendOpenAI} {
		b, reason := reg.Resolve(decisionFlags(t, fullyConfigured(name)))
		if b != nil || reason != decision.DecideFallbackUnavailable {
			t.Errorf("%s with a key set after startup: Resolve = (%v, %q), want (nil, %q)", name, b, reason, decision.DecideFallbackUnavailable)
		}
	}

	// A key removed after startup does not disable the backend either.
	t.Setenv("JEV_API_KEY", "")
	if b, reason := reg.Resolve(decisionFlags(t, fullyConfigured(flags.DecisionBackendJev))); b == nil {
		t.Errorf("jev lost its startup key after an env change: fallback %q", reason)
	}
}

// TestDecideBackends_KeyNeverInSpanAttributes: the configured API key reaches the
// wire as a header only; no attribute of any span the backends emit carries it,
// with content capture on and on an error answer.
// UC: UC-044 C-016
func TestDecideBackends_KeyNeverInSpanAttributes(t *testing.T) {
	recorder := tracetest.NewSpanRecorder()
	prev := otel.GetTracerProvider()
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder)))
	t.Cleanup(func() { otel.SetTracerProvider(prev) })

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":{"message":"nope"}}`, http.StatusUnauthorized)
	}))
	defer srv.Close()

	reg := newDecideRegistry(allKeys, srv.Client())
	openaiFlags := fullyConfigured(flags.DecisionBackendOpenAI)
	openaiFlags["decision.openai.base_url"] = srv.URL + "/v1"
	jevFlags := fullyConfigured(flags.DecisionBackendJev)
	jevFlags["decision.jev.base_url"] = srv.URL
	backends := map[string]decide.Backend{}
	for name, strs := range map[string]map[string]string{"openai": openaiFlags, "jev": jevFlags} {
		cfg := decisionFlags(t, strs)
		cfg.CaptureContent = true
		b, reason := reg.Resolve(cfg)
		if b == nil {
			t.Fatalf("%s: Resolve fell back: %q", name, reason)
		}
		backends[name] = b
	}
	backends["claude"] = claude.New(claude.Config{
		APIKey: allKeys.AnthropicAPIKey, Model: "claude-haiku-4-5-20251001", BaseURL: srv.URL,
		HTTPClient: srv.Client(), CaptureContent: true,
	})

	req := decide.Request{Questions: []decide.Question{{ID: decide.QuestionFFAIntervention}}}
	for _, b := range backends {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, _ = b.Decide(ctx, req)
		cancel()
	}

	ended := recorder.Ended()
	if len(ended) == 0 {
		t.Fatal("no spans recorded: the check would pass vacuously")
	}
	for _, sp := range ended {
		for _, kv := range sp.Attributes() {
			for _, secret := range []string{allKeys.AnthropicAPIKey, allKeys.OpenAIAPIKey, allKeys.JevAPIKey} {
				if strings.Contains(kv.Value.Emit(), secret) {
					t.Errorf("span %q attribute %s carries an API key", sp.Name(), kv.Key)
				}
			}
		}
		for _, ev := range sp.Events() {
			for _, kv := range ev.Attributes {
				for _, secret := range []string{allKeys.AnthropicAPIKey, allKeys.OpenAIAPIKey, allKeys.JevAPIKey} {
					if strings.Contains(kv.Value.Emit(), secret) {
						t.Errorf("span %q event %q attribute %s carries an API key", sp.Name(), ev.Name, kv.Key)
					}
				}
			}
		}
	}
}

// flagdDir holds the shipped flag files (services/flagd, plus the CI set in ci/).
const flagdDir = "../flagd"

// secretKeyName matches a JSON key or variant name that names a credential.
var secretKeyName = regexp.MustCompile(`(?i)(api[_.-]?key|secret|password|passwd|bearer|authorization|credential|access[_.-]?token)`)

// secretValue matches a string value shaped like an API key or bearer token:
// OpenAI / Anthropic (sk-..., sk-ant-...), GitHub, AWS, Google, a JWT, a Bearer
// header, or an opaque run of 32+ letters and digits.
var secretValue = regexp.MustCompile(`(?i)(\bsk-(ant-|proj-)?[a-z0-9_-]{16,}|\bgh[pousr]_[a-z0-9]{20,}|\bAKIA[0-9A-Z]{16}\b|\bAIza[0-9a-z_-]{30,}|\beyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}|\bbearer\s+\S{8,}|^[a-z0-9]{32,}$)`)

// secretsInFlagJSON walks one flag file and returns every JSON key or string
// value that looks like a credential, as "<path>: <why>" findings.
func secretsInFlagJSON(raw []byte) ([]string, error) {
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, err
	}
	var findings []string
	var walk func(path string, v any)
	walk = func(path string, v any) {
		switch x := v.(type) {
		case map[string]any:
			for k, child := range x {
				if secretKeyName.MatchString(k) {
					findings = append(findings, path+"/"+k+": credential-shaped key")
				}
				walk(path+"/"+k, child)
			}
		case []any:
			for i, child := range x {
				walk(fmt.Sprintf("%s[%d]", path, i), child)
			}
		case string:
			if secretValue.MatchString(x) {
				findings = append(findings, path+": API-key-shaped value")
			}
		}
	}
	walk("", doc)
	return findings, nil
}

// shippedFlagFiles walks flagdDir (the base flag files and ci/) and returns every
// .json file, failing the test when the set is too small to be the shipped one
// (a scan over it would pass vacuously).
func shippedFlagFiles(t *testing.T) []string {
	t.Helper()
	var files []string
	err := filepath.WalkDir(flagdDir, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() && strings.HasSuffix(path, ".json") {
			files = append(files, path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk %s: %v", flagdDir, err)
	}
	if len(files) < 8 {
		t.Fatalf("found %d flag files under %s, want the shipped set (the scan would pass vacuously)", len(files), flagdDir)
	}
	return files
}

// TestFlagFiles_HoldNoSecrets: no flag file the stack ships (services/flagd and
// services/flagd/ci) holds an API-key-shaped key or value, so a key can only come
// from the environment (C-016, UC-044 BR-001).
// UC: UC-044 C-016 BR-001
func TestFlagFiles_HoldNoSecrets(t *testing.T) {
	t.Run("UC-044/C-016 the scanner flags planted keys and passes clean flags", func(t *testing.T) {
		planted := map[string]string{
			"anthropic value": `{"flags":{"decision.claude.model":{"variants":{"x":"sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv"}}}}`,
			"openai value":    `{"flags":{"x":{"variants":{"on":"sk-proj-AbCdEfGhIjKlMnOpQrSt"}}}}`,
			"opaque value":    `{"flags":{"x":{"variants":{"on":"0123456789abcdef0123456789abcdef"}}}}`,
			"bearer value":    `{"flags":{"x":{"variants":{"on":"Bearer abcdefgh12345678"}}}}`,
			"key name":        `{"flags":{"decision.jev.api_key":{"variants":{"on":"x"}}}}`,
		}
		for name, doc := range planted {
			got, err := secretsInFlagJSON([]byte(doc))
			if err != nil || len(got) == 0 {
				t.Errorf("%s: findings = %v (err %v), want the planted secret found", name, got, err)
			}
		}
		clean := `{"flags":{"decision.claude.model":{"variants":{"haiku":"claude-haiku-4-5-20251001"}},` +
			`"decision.openai.base_url":{"variants":{"openai":"https://api.openai.com/v1"}},` +
			`"llm.max_tokens":{"variants":{"default":512}}}}`
		if got, err := secretsInFlagJSON([]byte(clean)); err != nil || len(got) != 0 {
			t.Errorf("clean flags: findings = %v (err %v), want none", got, err)
		}
	})

	t.Run("UC-044/C-016 no shipped flag file holds a key-shaped key or value", func(t *testing.T) {
		for _, f := range shippedFlagFiles(t) {
			raw, err := os.ReadFile(f)
			if err != nil {
				t.Fatalf("read %s: %v", f, err)
			}
			findings, err := secretsInFlagJSON(raw)
			if err != nil {
				t.Fatalf("parse %s: %v", f, err)
			}
			for _, finding := range findings {
				t.Errorf("%s%s", f, finding)
			}
		}
	})
}

// TestFlagFiles_BaseURLVariantsAreKeySafe: every variant of every *base_url flag
// in the shipped flag files (base and ci/) is key-safe, so selecting one (a CI-only
// `fake` variant in the dry-run stack, which carries real keys) can never send a
// key in clear text to a remote host (#1814).
// UC: UC-044 BR-003
// UC: C-016
func TestFlagFiles_BaseURLVariantsAreKeySafe(t *testing.T) {
	checked := 0
	for _, f := range shippedFlagFiles(t) {
		raw, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("read %s: %v", f, err)
		}
		var doc struct {
			Flags map[string]struct {
				Variants map[string]any `json:"variants"`
			} `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("parse %s: %v", f, err)
		}
		for key, flag := range doc.Flags {
			if !strings.HasSuffix(key, "base_url") {
				continue
			}
			checked++
			for variant, v := range flag.Variants {
				s, _ := v.(string)
				if reason, ok := keysafe.BaseURL(s); !ok {
					t.Errorf("UC-044/BR-003 %s: %s variant %q = %q is not key-safe (%s)", f, key, variant, s, reason)
				}
			}
		}
	}
	// decision.openai/jev/claude.base_url, in the base set and in ci/.
	if checked < 6 {
		t.Fatalf("checked %d base_url flags, want at least 6 (the scan would pass vacuously)", checked)
	}
}

// TestDecideRegistry_RefusesClearTextRemoteBaseURL: an openai, jev or claude base URL that
// is plain http to a non-loopback host resolves to backend_unavailable, so rules
// decide and no backend (hence no request carrying the key) exists. Loopback http
// stays available (the service tier's fakes).
// UC: UC-044 A1 BR-003
// UC: C-016
func TestDecideRegistry_RefusesClearTextRemoteBaseURL(t *testing.T) {
	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	for _, backend := range []string{flags.DecisionBackendOpenAI, flags.DecisionBackendJev, flags.DecisionBackendClaude} {
		key := "decision." + backend + ".base_url"
		suffix := "/v1" // claude's base URL has none: the SDK appends /v1/messages
		if backend == flags.DecisionBackendClaude {
			suffix = ""
		}
		for _, raw := range []string{"http://fake-openai:8080" + suffix, "http://192.168.1.5" + suffix, "https://u:p@api.example.test" + suffix} {
			t.Run("UC-044/A1 "+backend+" refuses "+raw, func(t *testing.T) {
				strs := fullyConfigured(backend)
				strs[key] = raw
				b, reason := reg.Resolve(decisionFlags(t, strs))
				if b != nil || reason != decision.DecideFallbackUnavailable {
					t.Errorf("Resolve = (%v, %q), want (nil, %q)", b, reason, decision.DecideFallbackUnavailable)
				}
			})
		}
		t.Run("UC-044/BR-003 "+backend+" accepts loopback http", func(t *testing.T) {
			strs := fullyConfigured(backend)
			strs[key] = "http://127.0.0.1:1" + suffix
			if b, reason := reg.Resolve(decisionFlags(t, strs)); b == nil {
				t.Errorf("loopback http fell back: %q", reason)
			}
		})
	}
}

// TestDecideHTTPClient_FollowsNoRedirect: the shared decide client returns a
// redirect as the response instead of following it, so a backend endpoint cannot
// forward the key to another host or downgrade it to plain http. Checked for
// every remote backend: the redirect target sees no request at all.
// UC: UC-044 BR-003
// UC: C-016
func TestDecideHTTPClient_FollowsNoRedirect(t *testing.T) {
	var (
		mu       sync.Mutex
		received []string
		hits     int
	)
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		received = append(received, r.URL.Path+" auth="+r.Header.Get("Authorization")+r.Header.Get("X-Api-Key"))
		mu.Unlock()
		http.Error(w, "{}", http.StatusUnauthorized)
	}))
	defer target.Close()
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits++
		mu.Unlock()
		http.Redirect(w, r, target.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	defer redirector.Close()

	reg := newDecideRegistry(allKeys, newDecideHTTPClient())
	// A question every backend encodes and sends: a backend that errors before
	// the network would otherwise pass this test vacuously (the hit count below).
	req := decide.Request{Questions: []decide.Question{{
		ID: decide.QuestionFFAIntervention, Kind: decide.KindChoice, Text: "Intervene?",
		Options: []string{"none", "boost"},
	}}}

	backends := map[string]func() decide.Backend{}
	// Every backend is resolved through the registry, so the test proves the
	// factory passes the shared no-redirect client AND the flag's base URL
	// (the hit-count check below). claude's URL has no /v1 (the SDK appends it).
	for _, name := range []string{flags.DecisionBackendClaude, flags.DecisionBackendOpenAI, flags.DecisionBackendJev} {
		backend := name
		backends[backend] = func() decide.Backend {
			strs := fullyConfigured(backend)
			if backend == flags.DecisionBackendClaude {
				strs["decision.claude.base_url"] = redirector.URL
			} else {
				strs["decision."+backend+".base_url"] = redirector.URL + "/v1"
			}
			b, reason := reg.Resolve(decisionFlags(t, strs))
			if b == nil {
				t.Fatalf("%s: Resolve fell back: %q", backend, reason)
			}
			return b
		}
	}
	for name, build := range backends {
		t.Run("UC-044/BR-003 "+name+" does not follow a redirect", func(t *testing.T) {
			mu.Lock()
			received = nil
			hits = 0
			mu.Unlock()
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			if _, err := build().Decide(ctx, req); err == nil {
				t.Error("a redirect answer must be an error")
			}
			mu.Lock()
			defer mu.Unlock()
			if hits != 1 {
				t.Errorf("redirector got %d requests, want exactly 1 (backend must reach the wire)", hits)
			}
			if len(received) != 0 {
				t.Errorf("redirect target received %v, want no request", received)
			}
		})
	}
}
