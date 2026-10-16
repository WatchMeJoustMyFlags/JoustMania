package inference

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/llm"
)

var recorder = tracetest.NewSpanRecorder()

func TestMain(m *testing.M) {
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	os.Exit(m.Run())
}

func lastChatSpan(t *testing.T) sdktrace.ReadOnlySpan {
	t.Helper()
	spans := recorder.Ended()
	for i := len(spans) - 1; i >= 0; i-- {
		if strings.HasPrefix(spans[i].Name(), "chat ") {
			return spans[i]
		}
	}
	t.Fatal("no chat span recorded")
	return nil
}

func spanAttrs(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	out := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		out[string(kv.Key)] = kv.Value
	}
	return out
}

// fakeMessages is a minimal Anthropic Messages API: it records each request body
// and header set and answers with the configured status and body.
type fakeMessages struct {
	mu      sync.Mutex
	status  int
	body    string
	delay   time.Duration
	bodies  []map[string]any
	headers []http.Header
}

func (f *fakeMessages) handler(w http.ResponseWriter, r *http.Request) {
	raw, _ := io.ReadAll(r.Body)
	var req map[string]any
	_ = json.Unmarshal(raw, &req)
	f.mu.Lock()
	f.bodies = append(f.bodies, req)
	f.headers = append(f.headers, r.Header.Clone())
	status, body, delay := f.status, f.body, f.delay
	f.mu.Unlock()
	if delay > 0 {
		select {
		case <-time.After(delay):
		case <-r.Context().Done():
			return
		}
	}
	w.Header().Set("Content-Type", "application/json")
	if status == 0 {
		status = http.StatusOK
	}
	w.WriteHeader(status)
	_, _ = io.WriteString(w, body)
}

func (f *fakeMessages) last(t *testing.T) (map[string]any, http.Header) {
	t.Helper()
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.bodies) == 0 {
		t.Fatal("no request reached the fake API")
	}
	return f.bodies[len(f.bodies)-1], f.headers[len(f.headers)-1]
}

func messageBody(text, stopReason string) string {
	b, _ := json.Marshal(map[string]any{
		"id":            "msg_test",
		"type":          "message",
		"role":          "assistant",
		"model":         "claude-opus-5-5",
		"stop_reason":   stopReason,
		"stop_sequence": nil,
		"content":       []map[string]any{{"type": "text", "text": text}},
		"usage": map[string]any{
			"input_tokens": 120, "output_tokens": 30,
			"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
		},
	})
	return string(b)
}

func newTestClaude(t *testing.T, f *fakeMessages, cfg Config) *Claude {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(f.handler))
	t.Cleanup(srv.Close)
	if cfg.APIKey == "" {
		cfg.APIKey = "sk-test"
	}
	cfg.BaseURL = srv.URL
	cfg.HTTPClient = srv.Client()
	return NewClaude(cfg)
}

func TestClaude_InferReturnsText(t *testing.T) {
	f := &fakeMessages{body: messageBody(`{"ok":true}`, "end_turn")}
	c := newTestClaude(t, f, Config{Effort: "medium"})

	got, err := c.Infer(context.Background(), llm.Prompt{System: "sys text", User: "user text", Model: "ignored"})
	if err != nil {
		t.Fatalf("Infer: %v", err)
	}
	if got != `{"ok":true}` {
		t.Fatalf("Infer = %q", got)
	}

	req, hdr := f.last(t)
	if req["model"] != DefaultModel {
		t.Errorf("model = %v, want %s (prompt.Model must not override the backend model)", req["model"], DefaultModel)
	}
	if req["max_tokens"] != float64(MaxTokens) {
		t.Errorf("max_tokens = %v", req["max_tokens"])
	}
	sys, _ := json.Marshal(req["system"])
	if !strings.Contains(string(sys), "sys text") {
		t.Errorf("system = %s", sys)
	}
	msgs, _ := json.Marshal(req["messages"])
	if !strings.Contains(string(msgs), "user text") {
		t.Errorf("messages = %s", msgs)
	}
	oc, _ := req["output_config"].(map[string]any)
	if oc["effort"] != "medium" {
		t.Errorf("output_config = %v, want effort medium", req["output_config"])
	}
	if req["fallbacks"] != "default" {
		t.Errorf("fallbacks = %v, want \"default\" on %s", req["fallbacks"], DefaultModel)
	}
	if !strings.Contains(hdr.Get("Anthropic-Beta"), "server-side-fallback-2026-07-01") {
		t.Errorf("anthropic-beta = %q", hdr.Get("Anthropic-Beta"))
	}
	if hdr.Get("X-Api-Key") != "sk-test" {
		t.Errorf("x-api-key = %q", hdr.Get("X-Api-Key"))
	}
	if hdr.Get("Traceparent") == "" {
		t.Error("traceparent not injected")
	}
	if _, ok := req["thinking"]; ok {
		t.Error("thinking must not be sent")
	}

	span := lastChatSpan(t)
	a := spanAttrs(span)
	for key, want := range map[string]string{
		"gen_ai.operation.name":          "chat",
		"gen_ai.provider.name":           "anthropic",
		"gen_ai.request.model":           DefaultModel,
		"gen_ai.response.model":          "claude-opus-5-5",
		"gen_ai.response.id":             "msg_test",
		"gen_ai.request.reasoning.level": "medium",
		"llm.prompt.system_sha256":       systemSHA("sys text"),
	} {
		if a[key].AsString() != want {
			t.Errorf("%s = %q, want %q", key, a[key].AsString(), want)
		}
	}
	if a["gen_ai.usage.input_tokens"].AsInt64() != 120 || a["gen_ai.usage.output_tokens"].AsInt64() != 30 {
		t.Errorf("usage = %v / %v", a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"])
	}
	if span.Status().Code == codes.Error {
		t.Errorf("status = %v", span.Status())
	}
}

func TestClaude_HaikuGetsNoEffortNoFallback(t *testing.T) {
	f := &fakeMessages{body: messageBody("x", "end_turn")}
	c := newTestClaude(t, f, Config{Model: "claude-haiku-4-5", Effort: "medium"})
	if _, err := c.Infer(context.Background(), llm.Prompt{User: "u"}); err != nil {
		t.Fatal(err)
	}
	req, hdr := f.last(t)
	if _, ok := req["output_config"]; ok {
		t.Errorf("output_config sent to haiku: %v", req["output_config"])
	}
	if _, ok := req["fallbacks"]; ok {
		t.Errorf("fallbacks sent to haiku: %v", req["fallbacks"])
	}
	if strings.Contains(hdr.Get("Anthropic-Beta"), "fallback") {
		t.Errorf("fallback beta sent to haiku: %q", hdr.Get("Anthropic-Beta"))
	}
	if _, ok := req["system"]; ok {
		t.Error("empty system prompt must be omitted")
	}
}

func TestClaude_UnavailableWithoutKey(t *testing.T) {
	c := NewClaude(Config{})
	if c.Available() {
		t.Fatal("Available without key")
	}
	if _, err := c.Infer(context.Background(), llm.Prompt{User: "u"}); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("err = %v, want ErrUnavailable", err)
	}
	if c.Model() != DefaultModel || c.Name() != Name {
		t.Fatalf("defaults: model %q name %q", c.Model(), c.Name())
	}
}

// TestClaude_ErrorTextNeverOnSpans pins #1242 for this client: a vendor error body
// reaches the caller's error but never the span (only error.type does).
func TestClaude_ErrorTextNeverOnSpans(t *testing.T) {
	const secret = "sk-ant-LEAKED-ECHO"
	f := &fakeMessages{status: http.StatusBadRequest,
		body: `{"type":"error","error":{"type":"invalid_request_error","message":"bad key ` + secret + `"}}`}
	c := newTestClaude(t, f, Config{})
	_, err := c.Infer(context.Background(), llm.Prompt{User: "u"})
	if err == nil {
		t.Fatal("want error")
	}
	span := lastChatSpan(t)
	if span.Status().Code != codes.Error || span.Status().Description != "400" {
		t.Errorf("status = %+v, want Error/400", span.Status())
	}
	if spanAttrs(span)["error.type"].AsString() != "400" {
		t.Errorf("error.type = %v", spanAttrs(span)["error.type"])
	}
	for _, kv := range span.Attributes() {
		if strings.Contains(kv.Value.Emit(), secret) {
			t.Errorf("attribute %s leaks the error body", kv.Key)
		}
	}
	for _, ev := range span.Events() {
		for _, kv := range ev.Attributes {
			if strings.Contains(kv.Value.Emit(), secret) {
				t.Errorf("event %s leaks the error body", ev.Name)
			}
		}
	}
}

func TestClaude_StopReasonsAndEmptyAreErrors(t *testing.T) {
	for _, tc := range []struct {
		name, text, stop, errType string
	}{
		{"refusal", "", "refusal", "refusal"},
		{"max_tokens", `{"trunc`, "max_tokens", "max_tokens"},
		{"empty", "  ", "end_turn", "empty_reply"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := &fakeMessages{body: messageBody(tc.text, tc.stop)}
			c := newTestClaude(t, f, Config{})
			if _, err := c.Infer(context.Background(), llm.Prompt{User: "u"}); err == nil {
				t.Fatal("want error")
			}
			if got := spanAttrs(lastChatSpan(t))["error.type"].AsString(); got != tc.errType {
				t.Errorf("error.type = %q, want %q", got, tc.errType)
			}
		})
	}
}

func TestClaude_Timeout(t *testing.T) {
	f := &fakeMessages{body: messageBody("late", "end_turn"), delay: 2 * time.Second}
	c := newTestClaude(t, f, Config{Timeout: 50 * time.Millisecond})
	start := time.Now()
	_, err := c.Infer(context.Background(), llm.Prompt{User: "u"})
	if err == nil {
		t.Fatal("want timeout error")
	}
	if time.Since(start) > time.Second {
		t.Fatalf("Infer took %v; the timeout did not bound it", time.Since(start))
	}
	if got := spanAttrs(lastChatSpan(t))["error.type"].AsString(); got != "timeout" {
		t.Errorf("error.type = %q, want timeout", got)
	}
}

func TestStripDateSuffix(t *testing.T) {
	for in, want := range map[string]string{
		"claude-haiku-4-5-20251001": "claude-haiku-4-5",
		"claude-opus-5-5":           "claude-opus-5-5",
		"x-1234567":                 "x-1234567",
	} {
		if got := stripDateSuffix(in); got != want {
			t.Errorf("stripDateSuffix(%q) = %q, want %q", in, got, want)
		}
	}
}

// --- the proposer seam, end to end over the fake API ---

func tempGameFile(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("..", "experiment", "testdata", "game.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	path := filepath.Join(t.TempDir(), "game.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return path
}

func gameVocab(path string) func() map[string]struct{} {
	return func() map[string]struct{} {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var doc struct {
			Flags map[string]any `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil
		}
		set := make(map[string]struct{}, len(doc.Flags))
		for k := range doc.Flags {
			set[k] = struct{}{}
		}
		return set
	}
}

// TestClaude_FeedsProposerSeam runs the real client (against the fake API)
// through the experiment Proposer and asserts a shadow experiment is proposed.
func TestClaude_FeedsProposerSeam(t *testing.T) {
	f := &fakeMessages{body: messageBody(
		`{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace may extend sessions"}`, "end_turn")}
	c := newTestClaude(t, f, Config{})

	path := tempGameFile(t)
	gate := experiment.NewGate(experiment.NewWriter(path, nil), nil, nil)
	proposer := experiment.NewProposer(c, gate, gameVocab(path), nil, nil)

	res := proposer.ProposeOnce(context.Background(), experiment.Narrative{GameMode: "joust"}, experiment.ProposerConfig{Engine: "claude"})
	if res.Outcome != experiment.ProposeProposed {
		t.Fatalf("outcome = %q, want proposed (err=%v)", res.Outcome, res.Err)
	}
	if res.Proposal == nil || res.Proposal.FlagKey != "death_grace_period_seconds" {
		t.Fatalf("proposal = %+v", res.Proposal)
	}
}
