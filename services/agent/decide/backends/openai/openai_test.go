package openai

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
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

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/gamecontext"
)

var recorder = tracetest.NewSpanRecorder()

func TestMain(m *testing.M) {
	otel.SetTracerProvider(sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder)))
	otel.SetTextMapPropagator(propagation.TraceContext{})
	os.Exit(m.Run())
}

// chatSpans returns the ended chat spans whose server.port matches port, oldest
// first (every test has its own httptest server, so its own port).
func chatSpans(t *testing.T, port int64) []sdktrace.ReadOnlySpan {
	t.Helper()
	var out []sdktrace.ReadOnlySpan
	for _, s := range recorder.Ended() {
		if strings.HasPrefix(s.Name(), "chat ") && attrs(s)["server.port"].AsInt64() == port {
			out = append(out, s)
		}
	}
	return out
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

// completion builds a /chat/completions response body. extra is merged into the
// message object (reasoning_content, refusal, ...).
func completion(finish string, content any, extra map[string]any) []byte {
	msg := map[string]any{"role": "assistant", "content": content}
	for k, v := range extra {
		msg[k] = v
	}
	b, _ := json.Marshal(map[string]any{
		"id":      "chatcmpl-test-123",
		"object":  "chat.completion",
		"model":   "gpt-4.1-mini-2025-04-14",
		"choices": []any{map[string]any{"index": 0, "message": msg, "finish_reason": finish}},
		"usage": map[string]any{
			"prompt_tokens":             900,
			"completion_tokens":         150,
			"prompt_tokens_details":     map[string]any{"cached_tokens": 100},
			"completion_tokens_details": map[string]any{"reasoning_tokens": 40},
		},
	})
	return b
}

type reply struct {
	status int
	body   []byte
	delay  time.Duration
}

// fakeAPI is an OpenAI-compatible endpoint. respond picks the reply from the
// decoded request body; it records every request body.
type fakeAPI struct {
	srv     *httptest.Server
	calls   atomic.Int32
	mu      sync.Mutex
	bodies  []map[string]any
	lastHdr http.Header
}

func newFakeAPI(t *testing.T, respond func(body map[string]any) reply) *fakeAPI {
	t.Helper()
	f := &fakeAPI{}
	f.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		f.mu.Lock()
		f.bodies = append(f.bodies, body)
		f.lastHdr = r.Header.Clone()
		f.mu.Unlock()
		if r.URL.Path != "/v1/chat/completions" || r.Method != http.MethodPost {
			http.Error(w, "bad path "+r.URL.Path, http.StatusNotFound)
			return
		}
		rep := respond(body)
		if rep.delay > 0 {
			select {
			case <-time.After(rep.delay):
			case <-r.Context().Done():
				return
			}
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(rep.status)
		_, _ = w.Write(rep.body)
	}))
	t.Cleanup(f.srv.Close)
	return f
}

func fixed(status int, body []byte) func(map[string]any) reply {
	return func(map[string]any) reply { return reply{status: status, body: body} }
}

func (f *fakeAPI) backend(model string, capture bool) *Backend {
	return New(Config{
		APIKey:         "sk-test",
		BaseURL:        f.srv.URL + "/v1/",
		Model:          model,
		Timeout:        2 * time.Second,
		HTTPClient:     f.srv.Client(),
		CaptureContent: capture,
	})
}

func (f *fakeAPI) body(i int) map[string]any {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.bodies[i]
}

func (f *fakeAPI) spans(t *testing.T) []sdktrace.ReadOnlySpan {
	u, err := url.Parse(f.srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	port, _ := strconv.ParseInt(u.Port(), 10, 64)
	return chatSpans(t, port)
}

func TestDecide_HappyPath(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	b := api.backend("gpt-4.1-mini", false)

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

	// Request shape. An unknown host (127.0.0.1) derives provider "openai", so
	// this is the OpenAI non-reasoning shape.
	body := api.body(0)
	if body["model"] != "gpt-4.1-mini" || body["stream"] != false {
		t.Errorf("model/stream = %v/%v", body["model"], body["stream"])
	}
	msgs := body["messages"].([]any)
	if len(msgs) != 2 || msgs[0].(map[string]any)["role"] != "system" || msgs[1].(map[string]any)["role"] != "user" {
		t.Errorf("messages = %v", msgs)
	}
	rf := body["response_format"].(map[string]any)
	js, _ := rf["json_schema"].(map[string]any)
	if rf["type"] != "json_schema" || js["name"] != schemaName || js["strict"] != true || js["schema"] == nil {
		t.Errorf("response_format = %v", rf)
	}
	if body["temperature"] != 0.0 || body["max_completion_tokens"].(float64) != MaxTokens {
		t.Errorf("temperature/max_completion_tokens = %v/%v", body["temperature"], body["max_completion_tokens"])
	}
	raw, _ := json.Marshal(body)
	if strings.Contains(string(raw), "CONTEXT-SENTINEL") {
		t.Error("request leaked Request.Context")
	}
	if got := api.lastHdr.Get("Authorization"); got != "Bearer sk-test" {
		t.Errorf("Authorization = %q", got)
	}

	// Span.
	spans := api.spans(t)
	if len(spans) != 1 {
		t.Fatalf("%d chat spans, want 1", len(spans))
	}
	s := spans[0]
	if s.Name() != "chat gpt-4.1-mini" || s.SpanKind() != trace.SpanKindClient {
		t.Errorf("span = %q kind %v", s.Name(), s.SpanKind())
	}
	if s.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Error("chat span is not a child of the caller's span")
	}
	tp := api.lastHdr.Get("Traceparent")
	if !strings.Contains(tp, s.SpanContext().TraceID().String()) || !strings.Contains(tp, s.SpanContext().SpanID().String()) {
		t.Errorf("traceparent %q does not carry the chat span", tp)
	}
	a := attrs(s)
	want := map[string]any{
		"gen_ai.operation.name":                "chat",
		"gen_ai.provider.name":                 "openai",
		"gen_ai.request.model":                 "gpt-4.1-mini",
		"gen_ai.response.model":                "gpt-4.1-mini-2025-04-14",
		"gen_ai.response.id":                   "chatcmpl-test-123",
		"gen_ai.request.max_tokens":            int64(MaxTokens),
		"gen_ai.request.temperature":           0.0,
		"gen_ai.output.type":                   "json",
		"gen_ai.usage.input_tokens":            int64(900),
		"gen_ai.usage.output_tokens":           int64(150),
		"gen_ai.usage.cache_read.input_tokens": int64(100),
		"gen_ai.usage.reasoning.output_tokens": int64(40),
		"server.address":                       "127.0.0.1",
		"decide.response_format":               "json_schema",
		"openai.api.type":                      "chat_completions",
	}
	for k, v := range want {
		if got, ok := a[k]; !ok || got.AsInterface() != v {
			t.Errorf("%s = %v, want %v", k, a[k].AsInterface(), v)
		}
	}
	if fr := a["gen_ai.response.finish_reasons"].AsStringSlice(); len(fr) != 1 || fr[0] != "stop" {
		t.Errorf("finish_reasons = %v", fr)
	}
	for _, k := range []string{"gen_ai.input.messages", "gen_ai.output.messages", "gen_ai.system_instructions", "error.type"} {
		if _, ok := a[k]; ok {
			t.Errorf("%s must not be set (capture off, success)", k)
		}
	}
	if s.Status().Code == codes.Error {
		t.Error("span status is Error on success")
	}
}

func TestDecide_ContentCapture(t *testing.T) {
	cases := map[string]map[string]any{
		"reasoning_content": {"reasoning_content": "Shield the weaker player."},
		"reasoning":         {"reasoning": "Shield the weaker player."},
	}
	for name, extra := range cases {
		t.Run(name, func(t *testing.T) {
			api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, extra)))
			if _, err := api.backend("deepseek-chat", true).Decide(context.Background(), testRequest()); err != nil {
				t.Fatalf("Decide: %v", err)
			}
			a := attrs(api.spans(t)[0])
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
				t.Errorf("reasoning part = %+v", parts[0])
			}
			if parts[1].Type != "reasoning" || !strings.HasPrefix(parts[1].Content, "MOCK0001 is close") {
				t.Errorf("rationale part = %+v", parts[1])
			}
			if parts[2].Type != "text" || parts[2].Content != answerJSON {
				t.Errorf("text part = %+v", parts[2])
			}
		})
	}
}

func TestDecide_ContentPartsArray(t *testing.T) {
	// Mistral reasoning models return content as typed parts.
	content := []any{
		map[string]any{"type": "thinking", "thinking": []any{map[string]any{"type": "text", "text": "Think first."}}},
		map[string]any{"type": "text", "text": answerJSON},
	}
	api := newFakeAPI(t, fixed(200, completion("stop", content, nil)))
	if _, err := api.backend("magistral-small", true).Decide(context.Background(), testRequest()); err != nil {
		t.Fatalf("Decide: %v", err)
	}
	var out []llmio.Message
	_ = json.Unmarshal([]byte(attrs(api.spans(t)[0])["gen_ai.output.messages"].AsString()), &out)
	if len(out) != 1 || len(out[0].Parts) != 3 || out[0].Parts[0].Content != "Think first." {
		t.Errorf("output = %+v", out)
	}
}

func TestDecide_JSONSchemaFallback(t *testing.T) {
	var schemaCalls atomic.Int32
	api := newFakeAPI(t, func(body map[string]any) reply {
		if body["response_format"].(map[string]any)["type"] == "json_schema" {
			schemaCalls.Add(1)
			return reply{status: 400, body: []byte(`{"error":{"message":"response_format json_schema is not supported","type":"invalid_request_error"}}`)}
		}
		return reply{status: 200, body: completion("stop", answerJSON, nil)}
	})
	b := api.backend("llama-3.3-70b", false)

	resp, err := b.Decide(context.Background(), testRequest())
	if err != nil {
		t.Fatalf("Decide: %v", err)
	}
	if a, _ := resp.Find(decide.QuestionFFAIntervention); a.Choice != "grant_shield" {
		t.Errorf("answer = %+v", a)
	}
	if n := api.calls.Load(); n != 2 {
		t.Fatalf("%d calls, want 2 (schema then object)", n)
	}
	second := api.body(1)["response_format"].(map[string]any)
	if second["type"] != "json_object" || second["json_schema"] != nil {
		t.Errorf("fallback response_format = %v", second)
	}
	spans := api.spans(t)
	if len(spans) != 2 {
		t.Fatalf("%d chat spans, want 2", len(spans))
	}
	if a := attrs(spans[0]); a["error.type"].AsString() != "400" || a["decide.response_format"].AsString() != "json_schema" {
		t.Errorf("first span = %v / %v", a["error.type"].AsInterface(), a["decide.response_format"].AsInterface())
	}
	if a := attrs(spans[1]); a["decide.response_format"].AsString() != "json_object" || spans[1].Status().Code == codes.Error {
		t.Errorf("second span = %v / %v", a["decide.response_format"].AsInterface(), spans[1].Status())
	}

	// Remembered on this instance ...
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	// ... and across instances for the same endpoint + model (the caller builds
	// a new Backend per decision).
	if _, err := api.backend("llama-3.3-70b", false).Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	if n := schemaCalls.Load(); n != 1 {
		t.Errorf("json_schema attempted %d times, want 1", n)
	}
	// A different model on the same endpoint still tries json_schema first.
	_, _ = api.backend("other-model", false).Decide(context.Background(), testRequest())
	if n := schemaCalls.Load(); n != 2 {
		t.Errorf("json_schema attempted %d times for a new model, want 2", n)
	}
}

func TestDecide_400NotAboutSchemaIsNotRemembered(t *testing.T) {
	// Both formats 400 (e.g. an unknown model): one fallback try, an error, and
	// no downgrade remembered.
	api := newFakeAPI(t, fixed(400, []byte(`{"error":{"message":"model not found"}}`)))
	b := api.backend("no-such-model", false)
	_, err := b.Decide(context.Background(), testRequest())
	if err == nil || errors.Is(err, decide.ErrInvalid) {
		t.Fatalf("err = %v", err)
	}
	if api.calls.Load() != 2 {
		t.Errorf("%d calls, want 2", api.calls.Load())
	}
	if b.schemaDowngraded() {
		t.Error("downgrade remembered although json_object also failed")
	}
	_, _ = b.Decide(context.Background(), testRequest())
	if api.body(2)["response_format"].(map[string]any)["type"] != "json_schema" {
		t.Error("next call did not retry json_schema")
	}
}

func TestDecide_Failures(t *testing.T) {
	huge := strings.Repeat("x", llmio.MaxOutputBytes+1)
	cases := []struct {
		name      string
		rep       reply
		timeout   time.Duration
		errType   string
		isInvalid bool
		calls     int32
	}{
		{name: "schema-violating output", errType: "invalid_output", isInvalid: true,
			rep: reply{status: 200, body: completion("stop", strings.Replace(answerJSON, `"choice":"grant_shield"`, `"choice":"eliminate"`, 1), nil)}},
		{name: "garbage json", errType: "invalid_output", isInvalid: true,
			rep: reply{status: 200, body: completion("stop", `{"answers": [ {"id": "ffa.int`, nil)}},
		{name: "prose instead of json", errType: "invalid_output", isInvalid: true,
			rep: reply{status: 200, body: completion("stop", "Grant a shield to MOCK0001.", nil)}},
		{name: "oversized model output", errType: "invalid_output", isInvalid: true,
			rep: reply{status: 200, body: completion("stop", huge, nil)}},
		{name: "oversized body", errType: "response_too_large", isInvalid: true,
			rep: reply{status: 200, body: []byte(`{"pad":"` + strings.Repeat("x", 1<<20) + `"}`)}},
		{name: "non-json body", errType: "invalid_response", isInvalid: true,
			rep: reply{status: 200, body: []byte("<html>gateway</html>")}},
		{name: "no choices", errType: "no_choices", isInvalid: true,
			rep: reply{status: 200, body: []byte(`{"id":"x","choices":[]}`)}},
		{name: "error envelope with 200", errType: "_OTHER",
			rep: reply{status: 200, body: []byte(`{"error":{"message":"quota"}}`)}},
		{name: "refusal", errType: "refusal", isInvalid: true,
			rep: reply{status: 200, body: completion("stop", nil, map[string]any{"refusal": "I can't help with that."})}},
		{name: "length", errType: "max_tokens", isInvalid: true,
			rep: reply{status: 200, body: completion("length", answerJSON[:40], nil)}},
		{name: "rate limited", errType: "429",
			rep: reply{status: 429, body: []byte(`{"error":{"message":"slow down"}}`)}},
		{name: "server error", errType: "500",
			rep: reply{status: 500, body: []byte(`{"error":{"message":"boom"}}`)}},
		{name: "timeout", timeout: 100 * time.Millisecond, errType: "timeout",
			rep: reply{status: 200, body: completion("stop", answerJSON, nil), delay: 2 * time.Second}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := newFakeAPI(t, func(map[string]any) reply { return tc.rep })
			b := api.backend("gpt-4.1-mini", true)
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
				t.Errorf("%d HTTP calls, want 1 (no retries)", n)
			}
			if tc.timeout > 0 && elapsed > tc.timeout+500*time.Millisecond {
				t.Errorf("timeout not honoured: took %v", elapsed)
			}
			spans := api.spans(t)
			if len(spans) != 1 {
				t.Fatalf("%d spans", len(spans))
			}
			if got := attrs(spans[0])["error.type"].AsString(); got != tc.errType {
				t.Errorf("error.type = %q, want %q", got, tc.errType)
			}
			if spans[0].Status().Code != codes.Error {
				t.Errorf("span status = %v, want Error", spans[0].Status())
			}
		})
	}
}

func TestAvailable(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	base := api.srv.URL + "/v1"
	cases := map[string]Config{
		"no key":      {BaseURL: base, Model: "m"},
		"blank key":   {APIKey: "  ", BaseURL: base, Model: "m"},
		"no model":    {APIKey: "k", BaseURL: base},
		"blank model": {APIKey: "k", BaseURL: base, Model: " "},
		"no base url": {APIKey: "k", Model: "m"},
		"blank base":  {APIKey: "k", Model: "m", BaseURL: " / "},
	}
	for name, cfg := range cases {
		t.Run(name, func(t *testing.T) {
			b := New(cfg)
			if b.Available() {
				t.Fatal("Available = true")
			}
			if _, err := b.Decide(context.Background(), testRequest()); !errors.Is(err, ErrUnavailable) {
				t.Errorf("err = %v, want ErrUnavailable", err)
			}
		})
	}
	if api.calls.Load() != 0 {
		t.Error("unavailable backend made an HTTP call")
	}
	if !New(Config{APIKey: "k", Model: "m", BaseURL: DefaultBaseURL}).Available() {
		t.Error("configured backend unavailable")
	}
	var nilBackend *Backend
	if nilBackend.Available() {
		t.Error("nil backend available")
	}
}

func TestNewDefaults(t *testing.T) {
	b := New(Config{APIKey: "k", Model: " gpt-4.1-mini ", BaseURL: " https://api.openai.com/v1/ "})
	if b.cfg.BaseURL != DefaultBaseURL || b.cfg.Timeout != DefaultTimeout || b.cfg.Model != "gpt-4.1-mini" {
		t.Errorf("defaults = %+v", b.cfg)
	}
	if b.host != "api.openai.com" || b.port != 443 || b.Provider() != "openai" {
		t.Errorf("server = %s:%d provider %s", b.host, b.port, b.Provider())
	}
	if b.cfg.HTTPClient != New(Config{}).cfg.HTTPClient {
		t.Error("default http client must be shared across New calls")
	}
	if New(Config{}).Name() != Name {
		t.Error("Name")
	}
}

func TestProviderName(t *testing.T) {
	cases := map[string]string{
		"https://api.openai.com/v1":                                   "openai",
		"https://generativelanguage.googleapis.com/v1beta/openai":     "gcp.gemini",
		"https://aiplatform.googleapis.com/v1/projects/p/endpoints/x": "gcp.vertex_ai",
		"https://us-central1-aiplatform.googleapis.com/v1beta1/x":     "gcp.vertex_ai",
		"https://api.mistral.ai/v1":                                   "mistral_ai",
		"https://api.groq.com/openai/v1":                              "groq",
		"https://api.deepseek.com":                                    "deepseek",
		"https://API.DeepSeek.com./v1":                                "deepseek",
		"https://api.x.ai/v1":                                         "x_ai",
		"https://api.perplexity.ai":                                   "perplexity",
		"https://api.cohere.ai/compatibility/v1":                      "cohere",
		"https://myres.openai.azure.com/openai/v1":                    "azure.ai.openai",
		"https://myres.services.ai.azure.com/models":                  "azure.ai.inference",
		"https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1":   "aws.bedrock",
		"https://us-south.ml.cloud.ibm.com/ml/v1":                     "ibm.watsonx.ai",
		"https://openrouter.ai/api/v1":                                "openai",
		"http://localhost:11434/v1":                                   "openai",
		"https://evil-api.openai.com.attacker.net/v1":                 "openai",
		"https://notapi.openai.com/v1":                                "openai",
		"":                                                            "openai",
		"::bad":                                                       "openai",
	}
	for base, want := range cases {
		if got := ProviderName(base); got != want {
			t.Errorf("ProviderName(%q) = %q, want %q", base, got, want)
		}
	}
}

func TestShapeFor(t *testing.T) {
	if s := shapeFor("openai", "gpt-5-mini", ""); !s.maxCompletionTokens || s.temperature != nil || s.reasoningEffort != "minimal" {
		t.Errorf("gpt-5 shape = %+v", s)
	}
	if s := shapeFor("openai", "o4-mini", ""); s.temperature != nil || s.reasoningEffort != "low" {
		t.Errorf("o4 shape = %+v", s)
	}
	if s := shapeFor("openai", "gpt-5-chat-latest", ""); s.temperature == nil || s.reasoningEffort != "" {
		t.Errorf("gpt-5-chat shape = %+v", s)
	}
	if s := shapeFor("openai", "gpt-4.1-mini", ""); !s.maxCompletionTokens || s.temperature == nil || s.reasoningEffort != "" {
		t.Errorf("gpt-4.1 shape = %+v", s)
	}
	if s := shapeFor("openai", "GPT-5", ""); s.temperature != nil || s.reasoningEffort != "minimal" {
		t.Errorf("upper-case GPT-5 shape = %+v, want reasoning + minimal", s)
	}
	if s := shapeFor("openai", "GPT-6-Mini", ""); s.temperature != nil || s.reasoningEffort != "low" {
		t.Errorf("upper-case GPT-6 shape = %+v, want reasoning + low", s)
	}
	if s := shapeFor("openai", "gpt-5-pro", "omit"); !s.maxCompletionTokens || s.temperature != nil || s.reasoningEffort != "" {
		t.Errorf("omit shape = %+v, want reasoning shape without reasoning_effort", s)
	}
	if s := shapeFor("groq", "gpt-5-oss", "minimal"); s.maxCompletionTokens || s.temperature == nil || s.reasoningEffort != "" {
		t.Errorf("non-openai shape = %+v", s)
	}
}

// TestIsReasoningModel: gpt-5 and every later generation (gpt-6*, gpt-10*) are
// reasoning models (no temperature) unless they are -chat variants.
func TestIsReasoningModel(t *testing.T) {
	cases := map[string]bool{
		"o1": true, "o3-mini": true, "o4-mini": true,
		"gpt-5": true, "gpt-5-mini": true, "gpt-5.1": true, "gpt-5.2-pro": true,
		"gpt-6": true, "gpt-6-mini": true, "gpt-7.5": true, "gpt-10": true, "gpt-12-nano": true,
		"gpt-5-chat-latest": false, "gpt-6-chat": false,
		"gpt-4.1-mini": false, "gpt-4o": false, "gpt-4": false, "gpt-3.5-turbo": false, "gpt-1": false,
	}
	for model, want := range cases {
		if got := isReasoningModel(model); got != want {
			t.Errorf("isReasoningModel(%q) = %v, want %v", model, got, want)
		}
		if s := shapeFor("openai", model, ""); (s.temperature == nil) != want {
			t.Errorf("shapeFor(%q).temperature = %v, want temperature only for non-reasoning", model, s.temperature)
		}
	}
}

// TestReasoningEffortFor: auto sends "minimal" only to the original gpt-5
// family and "low" to every other reasoning model; an explicit effort is sent
// verbatim.
func TestReasoningEffortFor(t *testing.T) {
	cases := []struct{ model, effort, want string }{
		{"gpt-5", "", "minimal"},
		{"gpt-5", "auto", "minimal"},
		{"gpt-5-mini", "auto", "minimal"},
		{"gpt-5-nano", "auto", "minimal"},
		{"gpt-5-2025-08-07", "auto", "minimal"},
		{"gpt-5-mini-2025-08-07", "auto", "minimal"},
		{"gpt-5.1", "auto", "low"},
		{"gpt-5.1-mini", "auto", "low"},
		{"gpt-5-pro", "auto", "low"},
		{"gpt-5-codex", "auto", "low"},
		{"gpt-6", "auto", "low"},
		{"gpt-6-mini", "auto", "low"},
		{"o3-mini", "auto", "low"},
		{"o4-mini", "", "low"},
		{"gpt-5.1", "none", "none"},
		{"gpt-5", "low", "low"},
		{"o4-mini", "minimal", "minimal"},
		{"o1-mini", "omit", ""},
		{"gpt-5-pro", "omit", ""},
	}
	for _, c := range cases {
		if got := reasoningEffortFor(c.model, c.effort); got != c.want {
			t.Errorf("reasoningEffortFor(%q, %q) = %q, want %q", c.model, c.effort, got, c.want)
		}
	}
}

// TestDecide_ReasoningEffortFlag: Config.ReasoningEffort (the
// decision.openai.reasoning_effort flag) is normalized and sent verbatim to a
// reasoning model, and never to a non-reasoning one.
func TestDecide_ReasoningEffortFlag(t *testing.T) {
	cases := []struct {
		model, effort string
		want          any // nil = field absent
	}{
		{"gpt-5.1", " None ", "none"},
		{"gpt-6", "auto", "low"},
		{"gpt-5-mini", "", "minimal"},
		{"gpt-4.1-mini", "low", nil},
		{"gpt-5-pro", " OMIT ", nil},
		{"GPT-5", "auto", "minimal"},
	}
	for _, c := range cases {
		api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
		b := New(Config{APIKey: "sk-test", BaseURL: api.srv.URL + "/v1", Model: c.model, ReasoningEffort: c.effort, HTTPClient: api.srv.Client()})
		b.provider = "openai"
		if _, err := b.Decide(context.Background(), testRequest()); err != nil {
			t.Fatalf("%s: %v", c.model, err)
		}
		got, ok := api.body(0)["reasoning_effort"]
		if c.want == nil {
			if ok {
				t.Errorf("%s effort %q: got reasoning_effort %v, want the field omitted", c.model, c.effort, got)
			}
			continue
		}
		if got != c.want {
			t.Errorf("%s effort %q: reasoning_effort = %v, want %v", c.model, c.effort, got, c.want)
		}
	}
}

func TestDecide_OpenAIReasoningRequestShape(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	b := api.backend("gpt-5-mini", false)
	b.provider = "openai" // httptest host derives "openai" anyway; be explicit
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	body := api.body(0)
	if _, ok := body["temperature"]; ok {
		t.Error("reasoning model must not get temperature")
	}
	if _, ok := body["max_tokens"]; ok || body["max_completion_tokens"].(float64) != MaxTokens {
		t.Errorf("tokens = %v / %v", body["max_tokens"], body["max_completion_tokens"])
	}
	if body["reasoning_effort"] != "minimal" {
		t.Errorf("reasoning_effort = %v", body["reasoning_effort"])
	}
	if a := attrs(api.spans(t)[0]); a["gen_ai.request.reasoning.level"].AsString() != "minimal" {
		t.Errorf("reasoning.level = %v", a["gen_ai.request.reasoning.level"].AsInterface())
	}
}

func TestDecide_NonOpenAIVendorShape(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	b := api.backend("mistral-small-latest", false)
	b.provider = "mistral_ai"
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	body := api.body(0)
	if body["max_tokens"].(float64) != MaxTokens || body["max_completion_tokens"] != nil {
		t.Errorf("tokens = %v / %v", body["max_tokens"], body["max_completion_tokens"])
	}
	a := attrs(api.spans(t)[0])
	if a["gen_ai.provider.name"].AsString() != "mistral_ai" {
		t.Errorf("provider = %v", a["gen_ai.provider.name"].AsInterface())
	}
	if _, ok := a["openai.api.type"]; ok {
		t.Error("openai.api.type is for the openai provider only")
	}
}

func TestDecide_NoQuestions(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	_, err := api.backend("gpt-4.1-mini", false).Decide(context.Background(), decide.Request{})
	if !errors.Is(err, decide.ErrInvalid) || api.calls.Load() != 0 {
		t.Errorf("err = %v, calls = %d", err, api.calls.Load())
	}
}

// schemaThenObject answers json_schema requests with schemaStatus/schemaBody
// and json_object requests with objectReply, counting json_schema attempts.
func schemaThenObject(schemaCalls *atomic.Int32, schemaStatus int, schemaBody string, objectReply reply) func(map[string]any) reply {
	return func(body map[string]any) reply {
		if body["response_format"].(map[string]any)["type"] == "json_schema" {
			schemaCalls.Add(1)
			return reply{status: schemaStatus, body: []byte(schemaBody)}
		}
		return objectReply
	}
}

const unsupportedBody = `{"error":{"message":"This response_format type is unavailable now","type":"invalid_request_error"}}`

func TestDecide_ValidationFailure400IsNotRemembered(t *testing.T) {
	// Groq's one-off output validation failure must not downgrade for good.
	var schemaCalls atomic.Int32
	groq := `{"error":{"message":"Generated JSON does not match the expected schema. Please adjust your prompt.","type":"invalid_request_error","code":"json_validate_failed"}}`
	api := newFakeAPI(t, schemaThenObject(&schemaCalls, 400, groq, reply{status: 200, body: completion("stop", answerJSON, nil)}))
	b := api.backend("llama-validate", false)
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatalf("Decide: %v (the json_object retry should still answer)", err)
	}
	if b.schemaDowngraded() {
		t.Error("validation-failure 400 downgraded")
	}
	_, _ = b.Decide(context.Background(), testRequest())
	if schemaCalls.Load() != 2 {
		t.Errorf("json_schema attempts = %d, want 2", schemaCalls.Load())
	}
}

func TestDecide_422TreatedLike400(t *testing.T) {
	var schemaCalls atomic.Int32
	api := newFakeAPI(t, schemaThenObject(&schemaCalls, 422, `{"detail":"json_schema not supported"}`,
		reply{status: 200, body: completion("stop", answerJSON, nil)}))
	b := api.backend("vllm-422", false)
	if _, err := b.Decide(context.Background(), testRequest()); err != nil {
		t.Fatalf("Decide: %v", err)
	}
	if api.calls.Load() != 2 || !b.schemaDowngraded() {
		t.Errorf("calls = %d downgraded = %v", api.calls.Load(), b.schemaDowngraded())
	}
}

func TestDecide_RetryNon2xxIsNotRemembered(t *testing.T) {
	for _, status := range []int{401, 429, 500} {
		var schemaCalls atomic.Int32
		api := newFakeAPI(t, schemaThenObject(&schemaCalls, 400, unsupportedBody,
			reply{status: status, body: []byte(`{"error":{"message":"nope"}}`)}))
		b := api.backend("retry-fails", false)
		if _, err := b.Decide(context.Background(), testRequest()); err == nil {
			t.Fatalf("%d: Decide succeeded", status)
		}
		if b.schemaDowngraded() {
			t.Errorf("%d on the retry downgraded", status)
		}
	}
}

func TestDecide_DowngradeExpires(t *testing.T) {
	var schemaCalls atomic.Int32
	api := newFakeAPI(t, schemaThenObject(&schemaCalls, 400, unsupportedBody,
		reply{status: 200, body: completion("stop", answerJSON, nil)}))
	clock := time.Now()
	now = func() time.Time { return clock }
	t.Cleanup(func() { now = time.Now })

	if _, err := api.backend("ttl-model", false).Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	clock = clock.Add(schemaDowngradeTTL - time.Second)
	if b := api.backend("ttl-model", false); !b.schemaDowngraded() {
		t.Error("downgrade forgotten before the TTL")
	}
	clock = clock.Add(2 * time.Second)
	b := api.backend("ttl-model", false)
	if b.schemaDowngraded() {
		t.Error("downgrade still active after the TTL")
	}
	_, _ = b.Decide(context.Background(), testRequest())
	if schemaCalls.Load() != 2 {
		t.Errorf("json_schema attempts = %d, want 2 (re-probed after TTL)", schemaCalls.Load())
	}
}

func TestDecide_RetryGetsRemainingBudget(t *testing.T) {
	// The first attempt eats most of the budget; the json_object retry must
	// only get what is left and end as a timeout, not a fresh full budget.
	var schemaCalls atomic.Int32
	api := newFakeAPI(t, func(body map[string]any) reply {
		if body["response_format"].(map[string]any)["type"] == "json_schema" {
			schemaCalls.Add(1)
			return reply{status: 400, body: []byte(unsupportedBody), delay: 250 * time.Millisecond}
		}
		return reply{status: 200, body: completion("stop", answerJSON, nil), delay: 2 * time.Second}
	})
	b := api.backend("budget-model", false)
	b.cfg.Timeout = 400 * time.Millisecond
	start := time.Now()
	_, err := b.Decide(context.Background(), testRequest())
	elapsed := time.Since(start)
	if err == nil {
		t.Fatal("Decide succeeded")
	}
	if elapsed > b.cfg.Timeout+300*time.Millisecond {
		t.Errorf("took %v, budget %v", elapsed, b.cfg.Timeout)
	}
	spans := api.spans(t)
	if len(spans) != 2 {
		t.Fatalf("%d spans", len(spans))
	}
	if got := attrs(spans[1])["error.type"].AsString(); got != "timeout" {
		t.Errorf("retry error.type = %q, want timeout", got)
	}
	if b.schemaDowngraded() {
		t.Error("timed-out retry downgraded")
	}
}

func TestScrubErrorBody(t *testing.T) {
	api := newFakeAPI(t, fixed(401, []byte(`{"error":{"message":"Incorrect API key provided: sk-proj-abcDEF123456. Header was Bearer abc.def-123"}}`)))
	_, err := api.backend("gpt-4.1-mini", false).Decide(context.Background(), testRequest())
	if err == nil || strings.Contains(err.Error(), "sk-proj") || strings.Contains(err.Error(), "abc.def-123") {
		t.Errorf("err = %v", err)
	}
	if !strings.Contains(err.Error(), "[redacted]") {
		t.Errorf("err = %v", err)
	}
}

// TestDecide_NoulUnsupported: a noul question (#1229) is rejected before any
// network I/O with an error wrapping decide.ErrInvalid.
func TestDecide_NoulUnsupported(t *testing.T) {
	api := newFakeAPI(t, fixed(200, completion("stop", answerJSON, nil)))
	req := testRequest()
	req.Questions = append(req.Questions, decide.Question{ID: "close", Kind: decide.KindNoul, Text: "Is the game close?"})
	_, err := api.backend("gpt-4.1-mini", false).Decide(context.Background(), req)
	if !errors.Is(err, decide.ErrInvalid) || !errors.Is(err, llmio.ErrUnsupportedKind) || api.calls.Load() != 0 {
		t.Errorf("err = %v, calls = %d", err, api.calls.Load())
	}
}
