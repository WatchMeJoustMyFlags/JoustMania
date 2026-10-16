package jev

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

func attrs(s sdktrace.ReadOnlySpan) map[string]attribute.Value {
	out := map[string]attribute.Value{}
	for _, kv := range s.Attributes() {
		out[string(kv.Key)] = kv.Value
	}
	return out
}

// testRequest is the request the fixtures in testdata/ answer.
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
		if r.URL.Path != "/api"+wirePath {
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
		APIKey:         "jev-test-key",
		BaseURL:        f.srv.URL + "/api/",
		Model:          model,
		Timeout:        2 * time.Second,
		HTTPClient:     f.srv.Client(),
		CaptureContent: capture,
	})
}

// lastSpan returns the most recent chat span against this server.
func (f *fakeAPI) lastSpan(t *testing.T) sdktrace.ReadOnlySpan {
	t.Helper()
	u, _ := url.Parse(f.srv.URL)
	port, _ := strconv.ParseInt(u.Port(), 10, 64)
	spans := recorder.Ended()
	for i := len(spans) - 1; i >= 0; i-- {
		if strings.HasPrefix(spans[i].Name(), "chat") && attrs(spans[i])["server.port"].AsInt64() == port {
			return spans[i]
		}
	}
	t.Fatal("no chat span recorded")
	return nil
}

func TestDecide_HappyPathSpan(t *testing.T) {
	api := newFakeAPI(t, 200, loadFixture(t, "response_ok.json"), 0)
	ctx, parent := otel.Tracer("test").Start(context.Background(), "agent.decide")
	resp, err := api.backend(fixtureModel, false).Decide(ctx, testRequest())
	parent.End()
	if err != nil {
		t.Fatalf("Decide: %v", err)
	}
	if a, _ := resp.Find(decide.QuestionFFAIntervention); a.Choice != "grant_shield" {
		t.Errorf("answer = %+v", a)
	}
	if strings.Contains(string(api.lastBody.Load().([]byte)), "CONTEXT-SENTINEL") {
		t.Error("request leaked Request.Context")
	}
	hdr := api.lastHdr.Load().(http.Header)
	if hdr.Get("Authorization") != "Bearer jev-test-key" {
		t.Errorf("auth = %q", hdr.Get("Authorization"))
	}

	s := api.lastSpan(t)
	if s.Name() != "chat "+fixtureModel || s.SpanKind() != trace.SpanKindClient {
		t.Errorf("span = %q kind %v", s.Name(), s.SpanKind())
	}
	if s.Parent().SpanID() != parent.SpanContext().SpanID() {
		t.Error("chat span is not a child of the caller's span")
	}
	if tp := hdr.Get("Traceparent"); !strings.Contains(tp, s.SpanContext().SpanID().String()) {
		t.Errorf("traceparent %q does not carry the chat span", tp)
	}
	a := attrs(s)
	want := map[string]any{
		"gen_ai.operation.name":                "chat",
		"gen_ai.provider.name":                 "jev",
		"gen_ai.request.model":                 fixtureModel,
		"gen_ai.response.model":                "jev-test-model-2026-10",
		"gen_ai.response.id":                   "jev-resp-001",
		"gen_ai.request.max_tokens":            int64(wireMaxTokens),
		"gen_ai.request.temperature":           0.0,
		"gen_ai.output.type":                   "json",
		"gen_ai.usage.input_tokens":            int64(850),
		"gen_ai.usage.output_tokens":           int64(160),
		"gen_ai.usage.reasoning.output_tokens": int64(30),
		"server.address":                       "127.0.0.1",
	}
	for k, v := range want {
		if got, ok := a[k]; !ok || got.AsInterface() != v {
			t.Errorf("%s = %v, want %v", k, a[k].AsInterface(), v)
		}
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

func TestDecide_NoModelSpanName(t *testing.T) {
	api := newFakeAPI(t, 200, loadFixture(t, "response_ok.json"), 0)
	if _, err := api.backend("", false).Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	s := api.lastSpan(t)
	if s.Name() != "chat" {
		t.Errorf("span name = %q, want chat", s.Name())
	}
	if _, ok := attrs(s)["gen_ai.request.model"]; ok {
		t.Error("gen_ai.request.model set without a model")
	}
	var body map[string]any
	_ = json.Unmarshal(api.lastBody.Load().([]byte), &body)
	if _, ok := body["model"]; ok {
		t.Error("model sent although none is configured")
	}
}

func TestDecide_ContentCapture(t *testing.T) {
	api := newFakeAPI(t, 200, loadFixture(t, "response_ok.json"), 0)
	if _, err := api.backend(fixtureModel, true).Decide(context.Background(), testRequest()); err != nil {
		t.Fatal(err)
	}
	a := attrs(api.lastSpan(t))
	if !strings.Contains(a["gen_ai.input.messages"].AsString(), "ffa.intervention") || a["gen_ai.system_instructions"].AsString() == "" {
		t.Errorf("input content = %v", a["gen_ai.input.messages"].AsString())
	}
	var out []llmio.Message
	if err := json.Unmarshal([]byte(a["gen_ai.output.messages"].AsString()), &out); err != nil || len(out) != 1 {
		t.Fatalf("output.messages = %v (%v)", a["gen_ai.output.messages"].AsString(), err)
	}
	p := out[0].Parts
	if len(p) != 3 || p[0].Type != "reasoning" || p[1].Type != "reasoning" || p[2].Type != "text" || out[0].FinishReason != "stop" {
		t.Errorf("output message = %+v", out[0])
	}
	if !strings.HasPrefix(p[0].Content, "Two players left") || !strings.HasPrefix(p[1].Content, "MOCK0002 is one move") {
		t.Errorf("reasoning parts = %+v", p[:2])
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
		{name: "garbage body", status: 200, body: []byte("not json"), errType: "invalid_response", isInvalid: true},
		{name: "oversized body", status: 200, body: []byte(`{"x":"` + strings.Repeat("x", 1<<20) + `"}`), errType: "response_too_large", isInvalid: true},
		{name: "rate limited", status: 429, body: []byte(`{"error":{"message":"slow down"}}`), errType: "429"},
		{name: "server error", status: 500, body: []byte(`{"error":{"message":"boom"}}`), errType: "500"},
		{name: "timeout", status: 200, body: []byte(`{}`), delay: 2 * time.Second, timeout: 100 * time.Millisecond, errType: "timeout"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			api := newFakeAPI(t, tc.status, tc.body, tc.delay)
			b := api.backend(fixtureModel, true)
			if tc.timeout > 0 {
				b.cfg.Timeout = tc.timeout
			}
			start := time.Now()
			_, err := b.Decide(context.Background(), testRequest())
			if err == nil {
				t.Fatal("Decide succeeded")
			}
			if tc.isInvalid != errors.Is(err, decide.ErrInvalid) {
				t.Errorf("errors.Is(ErrInvalid) = %v, want %v (%v)", !tc.isInvalid, tc.isInvalid, err)
			}
			if tc.timeout > 0 && time.Since(start) > tc.timeout+500*time.Millisecond {
				t.Errorf("timeout not honoured: took %v", time.Since(start))
			}
			if api.calls.Load() != 1 {
				t.Errorf("%d calls, want 1", api.calls.Load())
			}
			s := api.lastSpan(t)
			if got := attrs(s)["error.type"].AsString(); got != tc.errType {
				t.Errorf("error.type = %q, want %q", got, tc.errType)
			}
			if s.Status().Code != codes.Error {
				t.Errorf("status = %v", s.Status())
			}
		})
	}
}

// TestAvailable: the stub state. Without JEV_API_KEY (or a base URL) the
// backend is unavailable and never touches the network, so the decision
// wiring falls back to rules with fallback_reason=unavailable.
func TestAvailable(t *testing.T) {
	api := newFakeAPI(t, 200, loadFixture(t, "response_ok.json"), 0)
	cases := map[string]Config{
		"zero config":   {},
		"no key":        {BaseURL: api.srv.URL + "/api", Model: "m"},
		"blank key":     {APIKey: " ", BaseURL: api.srv.URL + "/api"},
		"no base url":   {APIKey: "k", Model: "m"},
		"blank baseurl": {APIKey: "k", BaseURL: " "},
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
	if !New(Config{APIKey: "k", BaseURL: "https://jev.example/v1"}).Available() {
		t.Error("key + base URL (no model) must be available")
	}
	var nilBackend *Backend
	if nilBackend.Available() {
		t.Error("nil backend available")
	}
	b := New(Config{})
	if b.Name() != Name || b.cfg.Timeout != DefaultTimeout {
		t.Errorf("defaults = %s %v", b.Name(), b.cfg.Timeout)
	}
}

func TestDecide_NoQuestions(t *testing.T) {
	api := newFakeAPI(t, 200, loadFixture(t, "response_ok.json"), 0)
	_, err := api.backend(fixtureModel, false).Decide(context.Background(), decide.Request{})
	if !errors.Is(err, decide.ErrInvalid) || api.calls.Load() != 0 {
		t.Errorf("err = %v, calls = %d", err, api.calls.Load())
	}
}
