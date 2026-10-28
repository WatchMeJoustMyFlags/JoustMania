package claude

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/anthropics/anthropic-sdk-go"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/internal/anthropichttp"
)

var recorder = tracetest.NewSpanRecorder()

func TestMain(m *testing.M) {
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	os.Exit(m.Run())
}

// lastChatSpan returns the most recent ended chat span.
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

func attrs(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	out := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		out[string(kv.Key)] = kv.Value
	}
	return out
}

func testRequest() decide.Request {
	st := decide.State{
		GameID:    "game_abc",
		GameKind:  "real",
		Narrative: "game: id=game_abc kind=real\n",
		Players: []decide.PlayerState{
			{Serial: "MOCK0001", Alive: true},
			{Serial: "MOCK0002", Alive: true},
		},
	}
	return decide.Request{
		State:     st,
		Questions: decide.FFAQuestions(st, []string{"grant_shield"}),
		Context:   &gamecontext.GameContext{SessionID: "CONTEXT-SENTINEL"},
	}
}

const answerJSON = `{"answers":[` +
	`{"id":"ffa.intervention","choice":"grant_shield","score":0,"probabilities":[{"option":"grant_shield","p":0.8},{"option":"noop","p":0.2}],"confidence":0.9},` +
	`{"id":"ffa.target","choice":"MOCK0001","score":0,"probabilities":[{"option":"MOCK0001","p":1}],"confidence":0.85},` +
	`{"id":"ffa.magnitude","choice":"","score":0.4,"probabilities":[],"confidence":0.7}],` +
	`"rationale":"MOCK0001 is close to the threshold; a short shield keeps it tense."}`

// message builds an Anthropic Messages API response body.
func message(stopReason string, blocks ...map[string]any) []byte {
	b, _ := json.Marshal(map[string]any{
		"id":            "msg_test_123",
		"type":          "message",
		"role":          "assistant",
		"model":         "claude-haiku-4-5-20251001",
		"content":       blocks,
		"stop_reason":   stopReason,
		"stop_sequence": nil,
		"usage": map[string]any{
			"input_tokens":                800,
			"output_tokens":               150,
			"cache_read_input_tokens":     100,
			"cache_creation_input_tokens": 0,
			"output_tokens_details":       map[string]any{"thinking_tokens": thinkingTokens(blocks)},
		},
	})
	return b
}

func thinkingTokens(blocks []map[string]any) int {
	for _, b := range blocks {
		if b["type"] == "thinking" {
			return 40
		}
	}
	return 0
}

func refusalMessage(category string) []byte {
	var m map[string]any
	_ = json.Unmarshal(message("refusal", textBlock("")), &m)
	m["stop_details"] = map[string]any{"type": "refusal", "category": category, "explanation": "declined"}
	b, _ := json.Marshal(m)
	return b
}

func textBlock(s string) map[string]any { return map[string]any{"type": "text", "text": s} }

// fakeAPI records the last request and replies with handler's result.
type fakeAPI struct {
	srv      *httptest.Server
	calls    atomic.Int32
	lastBody atomic.Value // []byte
	lastHdr  atomic.Value // http.Header
}

func newFakeAPI(t *testing.T, status int, body []byte, delay time.Duration) *fakeAPI {
	t.Helper()
	f := &fakeAPI{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		b, _ := io.ReadAll(r.Body)
		f.lastBody.Store(b)
		f.lastHdr.Store(r.Header.Clone())
		if r.URL.Path != "/v1/messages" {
			http.Error(w, "bad path "+r.URL.Path, http.StatusNotFound)
			return
		}
		if delay > 0 {
			select {
			case <-time.After(delay):
			case <-r.Context().Done():
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write(body)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func (f *fakeAPI) backend(model string, capture bool) *Backend {
	return New(Config{
		APIKey:         "sk-test",
		BaseURL:        f.srv.URL,
		Model:          model,
		Timeout:        2 * time.Second,
		HTTPClient:     f.srv.Client(),
		CaptureContent: capture,
	})
}

func (f *fakeAPI) requestBody(t *testing.T) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(f.lastBody.Load().([]byte), &m); err != nil {
		t.Fatalf("request body: %v", err)
	}
	return m
}

func TestDecide_HappyPath(t *testing.T) {
	api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
	b := api.backend("claude-haiku-4-5", false)

	ctx, parent := otel.Tracer("test").Start(context.Background(), "agent.decide")
	resp, err := b.Decide(ctx, testRequest())
	parent.End()
	if err != nil {
		t.Fatalf("Decide: %v", err)
	}
	if resp.Backend != Name {
		t.Errorf("backend = %q", resp.Backend)
	}
	if a, _ := resp.Find(decide.QuestionFFAIntervention); a.Choice != "grant_shield" || a.Confidence != 0.9 {
		t.Errorf("intervention = %+v", a)
	}
	if !strings.HasPrefix(resp.Rationale, "MOCK0001 is close") {
		t.Errorf("rationale = %q", resp.Rationale)
	}

	// Request shape.
	body := api.requestBody(t)
	if body["model"] != "claude-haiku-4-5" || body["max_tokens"].(float64) != MaxTokensNoThinking {
		t.Errorf("model/max_tokens = %v/%v", body["model"], body["max_tokens"])
	}
	if body["temperature"] != 0.0 {
		t.Errorf("haiku temperature = %v, want 0", body["temperature"])
	}
	if _, ok := body["thinking"]; ok {
		t.Error("haiku request must not enable thinking")
	}
	oc := body["output_config"].(map[string]any)
	format := oc["format"].(map[string]any)
	if format["type"] != "json_schema" || format["schema"] == nil {
		t.Errorf("output_config.format = %v", format)
	}
	if _, ok := oc["effort"]; ok {
		t.Error("haiku does not take effort")
	}
	if sys := body["system"].([]any); len(sys) != 1 || sys[0].(map[string]any)["cache_control"] != nil {
		t.Errorf("haiku system = %v, want one block without cache_control", body["system"])
	}
	raw := string(api.lastBody.Load().([]byte))
	if strings.Contains(raw, "CONTEXT-SENTINEL") {
		t.Error("request leaked Request.Context")
	}
	hdr := api.lastHdr.Load().(http.Header)
	if hdr.Get("X-Api-Key") != "sk-test" {
		t.Error("api key header missing")
	}
	if hdr.Get("Anthropic-Version") == "" {
		t.Error("anthropic-version header missing")
	}

	// Span.
	s := lastChatSpan(t)
	if s.Name() != "chat claude-haiku-4-5" || s.SpanKind() != trace.SpanKindClient {
		t.Errorf("span = %q kind %v", s.Name(), s.SpanKind())
	}
	if s.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Error("chat span is not a child of the caller's span")
	}
	tp := hdr.Get("Traceparent")
	if !strings.Contains(tp, s.SpanContext().TraceID().String()) || !strings.Contains(tp, s.SpanContext().SpanID().String()) {
		t.Errorf("traceparent %q does not carry the chat span", tp)
	}
	a := attrs(s)
	want := map[string]any{
		"gen_ai.operation.name":                    "chat",
		"gen_ai.provider.name":                     "anthropic",
		"gen_ai.request.model":                     "claude-haiku-4-5",
		"gen_ai.response.model":                    "claude-haiku-4-5-20251001",
		"gen_ai.response.id":                       "msg_test_123",
		"gen_ai.request.max_tokens":                int64(MaxTokensNoThinking),
		"gen_ai.request.temperature":               0.0,
		"gen_ai.output.type":                       "json",
		"gen_ai.usage.input_tokens":                int64(900), // 800 + 100 cache read
		"gen_ai.usage.output_tokens":               int64(150),
		"gen_ai.usage.cache_read.input_tokens":     int64(100),
		"gen_ai.usage.cache_creation.input_tokens": int64(0),
		"server.address":                           "127.0.0.1",
	}
	for k, v := range want {
		if got, ok := a[k]; !ok || got.AsInterface() != v {
			t.Errorf("%s = %v, want %v", k, a[k].AsInterface(), v)
		}
	}
	if fr := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(fr) != 1 || fr[0] != "end_turn" {
		t.Errorf("finish_reasons = %v", fr)
	}
	if a["server.port"].AsInt64() == 0 {
		t.Error("server.port missing")
	}
	if sha := a["llm.prompt.system_sha256"].AsString(); len(sha) != 16 || sha != anthropichttp.SystemSHA(llmio.Render(testRequest()).System) {
		t.Errorf("llm.prompt.system_sha256 = %q", sha)
	}
	for _, k := range []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions",
		"error.type", "gen_ai.usage.reasoning.output_tokens", "gen_ai.request.reasoning.level"} {
		if _, ok := a[k]; ok {
			t.Errorf("%s must not be set (capture off, success)", k)
		}
	}
	if s.Status().Code == codes.Error {
		t.Error("span status is Error on success")
	}
	// The SDK's own instrumentation is off: exactly one span per call.
	for _, sp := range recorder.Ended() {
		if sp.Parent().SpanID() == s.SpanContext().SpanID() {
			t.Errorf("unexpected child span %q under the chat span (SDK instrumentation must be off)", sp.Name())
		}
	}
}

func TestDecide_ContentCapture(t *testing.T) {
	thinking := map[string]any{"type": "thinking", "thinking": "Shield the weaker player.", "signature": "sig"}
	api := newFakeAPI(t, 200, message("end_turn", thinking, textBlock(answerJSON)), 0)
	b := api.backend("claude-opus-5-5", true)
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatalf("Decide: %v", err)
	}
	a := attrs(lastChatSpan(t))

	var sys []llmio.Part
	if err := json.Unmarshal([]byte(a["gen_ai.system_instructions"].AsString()), &sys); err != nil || len(sys) != 1 {
		t.Fatalf("system_instructions = %v (%v)", a["gen_ai.system_instructions"].AsString(), err)
	}
	var in []llmio.Message
	if err := json.Unmarshal([]byte(a["gen_ai.input.messages"].AsString()), &in); err != nil ||
		len(in) != 1 || in[0].Role != "user" || !strings.Contains(in[0].Parts[0].Content, "ffa.intervention") {
		t.Fatalf("input.messages = %v (%v)", a["gen_ai.input.messages"].AsString(), err)
	}
	var out []llmio.Message
	if err := json.Unmarshal([]byte(a["gen_ai.output.messages"].AsString()), &out); err != nil || len(out) != 1 {
		t.Fatalf("output.messages = %v (%v)", a["gen_ai.output.messages"].AsString(), err)
	}
	parts := out[0].Parts
	if out[0].Role != "assistant" || out[0].FinishReason != "stop" || len(parts) != 3 {
		t.Fatalf("output message = %+v", out[0])
	}
	if parts[0].Type != "reasoning" || parts[0].Content != "Shield the weaker player." {
		t.Errorf("thinking part = %+v", parts[0])
	}
	if parts[1].Type != "reasoning" || !strings.HasPrefix(parts[1].Content, "MOCK0001 is close") {
		t.Errorf("rationale part = %+v", parts[1])
	}
	if parts[2].Type != "text" || parts[2].Content != answerJSON {
		t.Errorf("text part = %+v", parts[2])
	}

	// Opus 5.5: thinking cannot be disabled; low effort, no temperature.
	body := api.requestBody(t)
	if body["output_config"].(map[string]any)["effort"] != "low" {
		t.Errorf("effort = %v", body["output_config"])
	}
	if _, ok := body["temperature"]; ok {
		t.Error("opus 5.5 rejects temperature")
	}
	if _, ok := a["gen_ai.request.temperature"]; ok {
		t.Error("temperature attribute set although none was sent")
	}
	if a["gen_ai.request.reasoning.level"].AsString() != "low" {
		t.Errorf("reasoning.level = %v", a["gen_ai.request.reasoning.level"].AsInterface())
	}
	if a["gen_ai.usage.reasoning.output_tokens"].AsInt64() != 40 {
		t.Errorf("reasoning tokens = %v", a["gen_ai.usage.reasoning.output_tokens"].AsInterface())
	}
}

func TestDecide_Failures(t *testing.T) {
	cases := []struct {
		name      string
		status    int
		body      []byte
		delay     time.Duration
		timeout   time.Duration
		errType   string
		isInvalid bool
	}{
		{name: "schema-violating output", status: 200, errType: "invalid_output", isInvalid: true,
			body: message("end_turn", textBlock(strings.Replace(answerJSON, `"choice":"grant_shield"`, `"choice":"eliminate"`, 1)))},
		{name: "garbage json", status: 200, errType: "invalid_output", isInvalid: true,
			body: message("end_turn", textBlock(`{"answers": [ {"id": "ffa.int`))},
		{name: "prose instead of json", status: 200, errType: "invalid_output", isInvalid: true,
			body: message("end_turn", textBlock("Grant a shield to MOCK0001."))},
		{name: "refusal", status: 200, errType: "refusal", isInvalid: true,
			body: refusalMessage("cyber")},
		{name: "max tokens", status: 200, errType: "max_tokens", isInvalid: true,
			body: message("max_tokens", textBlock(answerJSON[:40]))},
		{name: "rate limited", status: 429, errType: "429",
			body: []byte(`{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}`)},
		{name: "server error", status: 500, errType: "500",
			body: []byte(`{"type":"error","error":{"type":"api_error","message":"boom"}}`)},
		{name: "overloaded", status: 529, errType: "529",
			body: []byte(`{"type":"error","error":{"type":"overloaded_error","message":"busy"}}`)},
		{name: "timeout", status: 200, delay: 2 * time.Second, timeout: 100 * time.Millisecond, errType: "timeout",
			body: message("end_turn", textBlock(answerJSON))},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := newFakeAPI(t, tc.status, tc.body, tc.delay)
			b := api.backend("claude-haiku-4-5", true)
			if tc.timeout > 0 {
				b.cfg.Timeout = tc.timeout
			}
			start := time.Now()
			_, err := b.Decide(context.Background(), testRequest())
			elapsed := time.Since(start)
			if err == nil {
				t.Fatal("Decide succeeded")
			}
			if tc.isInvalid != errors.Is(err, decide.ErrInvalid) {
				t.Errorf("errors.Is(ErrInvalid) = %v, want %v (%v)", !tc.isInvalid, tc.isInvalid, err)
			}
			if n := api.calls.Load(); n != 1 {
				t.Errorf("%d HTTP calls, want 1 (no SDK retries)", n)
			}
			if tc.timeout > 0 && elapsed > tc.timeout+500*time.Millisecond {
				t.Errorf("timeout not honoured: took %v", elapsed)
			}
			s := lastChatSpan(t)
			if got := attrs(s)["error.type"].AsString(); got != tc.errType {
				t.Errorf("error.type = %q, want %q", got, tc.errType)
			}
			if s.Status().Code != codes.Error {
				t.Errorf("span status = %v, want Error", s.Status())
			}
			a := attrs(s)
			if tc.status != 200 || tc.timeout > 0 {
				if fr := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(fr) != 1 || fr[0] != "error" {
					t.Errorf("transport failure finish_reasons = %v, want [error]", fr)
				}
			}
			if tc.errType == "refusal" && a["anthropic.refusal.category"].AsString() != "cyber" {
				t.Errorf("refusal category = %v", a["anthropic.refusal.category"].AsInterface())
			}
		})
	}
}

// UC: UC-040 main
func TestAvailable(t *testing.T) {
	cases := map[string]Config{
		"UC-040/main step 2 no key":      {Model: "claude-haiku-4-5"},
		"UC-040/main step 2 blank key":   {APIKey: "  ", Model: "claude-haiku-4-5"},
		"UC-040/main step 2 no model":    {APIKey: "sk-test"},
		"UC-040/main step 2 blank model": {APIKey: "sk-test", Model: " "},
	}
	for name, cfg := range cases {
		t.Run(name, func(t *testing.T) {
			api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
			cfg.BaseURL = api.srv.URL
			b := New(cfg)
			if b.Available() {
				t.Fatal("Available = true")
			}
			if _, err := b.Decide(context.Background(), testRequest()); !errors.Is(err, ErrUnavailable) {
				t.Errorf("err = %v, want ErrUnavailable", err)
			}
			if api.calls.Load() != 0 {
				t.Error("unavailable backend made an HTTP call")
			}
		})
	}
	if !New(Config{APIKey: "k", Model: "m"}).Available() {
		t.Error("configured backend unavailable")
	}
	var nilBackend *Backend
	if nilBackend.Available() {
		t.Error("nil backend available")
	}
}

func TestNewDefaults(t *testing.T) {
	b := New(Config{APIKey: "k", Model: " claude-haiku-4-5 "})
	if b.cfg.BaseURL != DefaultBaseURL || b.cfg.Timeout != DefaultTimeout || b.cfg.Model != "claude-haiku-4-5" {
		t.Errorf("defaults = %+v", b.cfg)
	}
	if b.host != "api.anthropic.com" || b.port != 443 {
		t.Errorf("server = %s:%d", b.host, b.port)
	}
	if b.cfg.HTTPClient != New(Config{}).cfg.HTTPClient {
		t.Error("default http client must be shared across New calls")
	}
	if h, p := anthropichttp.ServerAddress("http://proxy.local/anthropic/"); h != "proxy.local" || p != 80 {
		t.Errorf("serverAddress = %s:%d", h, p)
	}
	if h, p := anthropichttp.ServerAddress("::bad"); h != "" || p != 0 {
		t.Errorf("bad url = %s:%d", h, p)
	}
}

func TestShapeFor(t *testing.T) {
	sonnet := shapeFor("claude-sonnet-5-5")
	if sonnet.thinking.OfAdaptive == nil || sonnet.effort != "low" || sonnet.temperature != nil {
		t.Errorf("sonnet 5.5 shape = %+v", sonnet)
	}
	sonnet5 := shapeFor("claude-sonnet-5")
	if sonnet5.thinking.OfDisabled == nil || sonnet5.effort != "low" {
		t.Errorf("sonnet 5 shape = %+v", sonnet5)
	}
	if s := shapeFor("claude-haiku-4-5-20251001"); s.temperature == nil || s.effort != "" {
		t.Errorf("dated haiku shape = %+v", s)
	}
	if s := shapeFor("claude-opus-5-5"); s.effort != "low" || s.thinking.OfDisabled != nil {
		t.Errorf("opus 5.5 shape = %+v", s)
	}
	for _, m := range []string{"some-future-model", "claude-sonnet-5-6", "claude-opus-5-6", "claude-opus-5-5-fast",
		"claude-fable-5", "claude-fable-5-1", "claude-haiku-4-5-2025", "claude-sonnet-5-5-x20251001"} {
		if s := shapeFor(m); s.effort != "" || s.temperature != nil || s.thinking.OfDisabled != nil ||
			s.thinking.OfBetweenTools != nil || s.thinking.OfAdaptive != nil {
			t.Errorf("%s: unknown model must get the empty shape, got %+v", m, s)
		}
	}
	if s := shapeFor("claude-sonnet-5-5-20260901"); s.thinking.OfAdaptive == nil {
		t.Errorf("dated sonnet 5.5 shape = %+v", s)
	}
}

func TestDecide_SonnetRequestShape(t *testing.T) {
	api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
	if _, err := api.backend("claude-sonnet-5-5", false).Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	body := api.requestBody(t)
	if th, _ := body["thinking"].(map[string]any); th["type"] != "adaptive" || len(th) != 1 {
		t.Errorf("thinking = %v", body["thinking"])
	}
	if oc, _ := body["output_config"].(map[string]any); oc["effort"] != "low" {
		t.Errorf("output_config = %v, want effort low", body["output_config"])
	}
	if body["max_tokens"].(float64) != MaxTokens {
		t.Errorf("max_tokens = %v, want %d (adaptive may think)", body["max_tokens"], MaxTokens)
	}
	// No prompt caching (see the package doc, #1231): no measurable TTFT gain
	// at this prompt size.
	if raw := string(api.lastBody.Load().([]byte)); strings.Contains(raw, "cache_control") {
		t.Error("request must not carry cache_control")
	}
}

// TestShapeFor_MaxTokens: thinking-off shapes size max_tokens to the answer;
// shapes that may think (Sonnet 5.5 and Opus 5.5 adaptive, unknown models) keep
// MaxTokens.
func TestShapeFor_MaxTokens(t *testing.T) {
	cases := map[string]int64{
		"claude-haiku-4-5":  MaxTokensNoThinking,
		"claude-sonnet-5-5": MaxTokens,
		"claude-sonnet-5":   MaxTokensNoThinking,
		"claude-opus-5":     MaxTokensNoThinking,
		"claude-opus-5-5":   MaxTokens,
		"some-future-model": MaxTokens,
	}
	for model, want := range cases {
		if got := shapeFor(model).maxTokensOf(); got != want {
			t.Errorf("%s: max_tokens %d, want %d", model, got, want)
		}
	}
}

func TestDecide_NoQuestions(t *testing.T) {
	api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
	_, err := api.backend("claude-haiku-4-5", false).Decide(context.Background(), decide.Request{})
	if !errors.Is(err, decide.ErrInvalid) || api.calls.Load() != 0 {
		t.Errorf("err = %v, calls = %d", err, api.calls.Load())
	}
}

// TestNew_ConfigWinsOverEnvironment checks that ANTHROPIC_* env cannot redirect or
// re-authenticate the client. (It does not prove WithoutEnvironmentDefaults is set:
// explicit options already override env values; that option skips the env/profile
// I/O and the env-only token, federation and header sources.)
func TestNew_ConfigWinsOverEnvironment(t *testing.T) {
	t.Setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
	t.Setenv("ANTHROPIC_AUTH_TOKEN", "env-token")
	t.Setenv("ANTHROPIC_API_KEY", "env-key")
	api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
	if _, err := api.backend("claude-haiku-4-5", false).Decide(context.Background(), testRequest()); err != nil {
		t.Fatalf("Decide: %v (env leaked into the client?)", err)
	}
	hdr := api.lastHdr.Load().(http.Header)
	if hdr.Get("X-Api-Key") != "sk-test" || hdr.Get("Authorization") != "" {
		t.Errorf("auth headers = x-api-key %q, authorization %q", hdr.Get("X-Api-Key"), hdr.Get("Authorization"))
	}
}

func TestFinishReasonMapping(t *testing.T) {
	cases := map[string]string{
		"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
		"model_context_window_exceeded": "length", "refusal": "content_filter",
		"tool_use": "tool_call", "pause_turn": "error", "something_new": "error",
	}
	for in, want := range cases {
		if got := finishReason(anthropic.StopReason(in)); got != want {
			t.Errorf("%s -> %s, want %s", in, got, want)
		}
	}
}

// TestDecide_NoulUnsupported: a noul question (#1229) is rejected before any
// network I/O with an error wrapping decide.ErrInvalid.
// UC: UC-040 A12
func TestDecide_NoulUnsupported(t *testing.T) {
	api := newFakeAPI(t, 200, message("end_turn", textBlock(answerJSON)), 0)
	req := testRequest()
	req.Questions = append(req.Questions, decide.Question{ID: "close", Kind: decide.KindNoul, Text: "Is the game close?"})
	_, err := api.backend("claude-haiku-4-5", false).Decide(context.Background(), req)
	if !errors.Is(err, decide.ErrInvalid) || !errors.Is(err, llmio.ErrUnsupportedKind) || api.calls.Load() != 0 {
		t.Errorf("err = %v, calls = %d", err, api.calls.Load())
	}
}

// TestDecide_InjectedClientNeverFollowsRedirect: the redirect guard is part of
// the SDK client itself (anthropichttp.NewClient), not of the http.Client the
// caller passes in. Even an injected client that follows redirects must not
// forward x-api-key to another host: the 3xx is an error and the target sees no
// request (#2321).
// UC: UC-044 BR-003
func TestDecide_InjectedClientNeverFollowsRedirect(t *testing.T) {
	for _, status := range []int{http.StatusFound, http.StatusTemporaryRedirect} {
		var targetHits atomic.Int32
		target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			targetHits.Add(1)
			http.Error(w, "should not be reached", http.StatusTeapot)
		}))
		t.Cleanup(target.Close)
		redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, target.URL+r.URL.Path, status)
		}))
		t.Cleanup(redirector.Close)

		b := New(Config{
			APIKey:     "sk-redirect-secret",
			BaseURL:    redirector.URL,
			Model:      "claude-haiku-4-5",
			Timeout:    2 * time.Second,
			HTTPClient: &http.Client{}, // follows redirects by default
		})
		if _, err := b.Decide(context.Background(), testRequest()); err == nil {
			t.Errorf("status %d: Decide succeeded through a redirect, want an error", status)
		}
		if n := targetHits.Load(); n != 0 {
			t.Errorf("status %d: redirect target received %d request(s) (x-api-key leaked)", status, n)
		}
	}
}
