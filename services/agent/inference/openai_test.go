package inference

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/llm"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// sampleCompletion is a realistic OpenAI-compatible /chat/completions response body.
const sampleCompletion = `{
  "id": "chatcmpl-abc",
  "object": "chat.completion",
  "model": "phi4-mini",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "{\"flag\":\"foo\",\"value\":3,\"rationale\":\"tighter\"}"}, "finish_reason": "stop"}
  ]
}`

// newMockBackend points an OpenAIBackend at the test server and injects the
// server's client so no real network I/O happens.
func newMockBackend(srv *httptest.Server, apiKey, model string) *OpenAIBackend {
	return New(srv.URL+"/v1", apiKey, model, WithHTTPClient(srv.Client()))
}

func TestInfer_ParsesCompletionAndRequestShape(t *testing.T) {
	var gotMethod, gotPath, gotAuth, gotContentType string
	var gotBody chatRequest

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotMethod = r.Method
		gotPath = r.URL.Path
		gotAuth = r.Header.Get("Authorization")
		gotContentType = r.Header.Get("Content-Type")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &gotBody)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, sampleCompletion)
	}))
	defer srv.Close()

	b := newMockBackend(srv, "secret-key", "phi4-mini")
	out, err := b.Infer(context.Background(), llm.Prompt{System: "sys text", User: "user text"})
	if err != nil {
		t.Fatalf("Infer returned error: %v", err)
	}

	// Returned the raw model content verbatim (the decoder parses it downstream).
	wantContent := `{"flag":"foo","value":3,"rationale":"tighter"}`
	if out != wantContent {
		t.Errorf("content = %q, want %q", out, wantContent)
	}

	// Request shape.
	if gotMethod != http.MethodPost {
		t.Errorf("method = %s, want POST", gotMethod)
	}
	if gotPath != "/v1/chat/completions" {
		t.Errorf("path = %s, want /v1/chat/completions", gotPath)
	}
	if gotContentType != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", gotContentType)
	}
	if gotAuth != "Bearer secret-key" {
		t.Errorf("Authorization = %q, want Bearer secret-key", gotAuth)
	}
	if gotBody.Model != "phi4-mini" {
		t.Errorf("body model = %q, want phi4-mini", gotBody.Model)
	}
	if len(gotBody.Messages) != 2 {
		t.Fatalf("messages = %d, want 2 (system+user)", len(gotBody.Messages))
	}
	if gotBody.Messages[0].Role != "system" || gotBody.Messages[0].Content != "sys text" {
		t.Errorf("message[0] = %+v, want system/'sys text'", gotBody.Messages[0])
	}
	if gotBody.Messages[1].Role != "user" || gotBody.Messages[1].Content != "user text" {
		t.Errorf("message[1] = %+v, want user/'user text'", gotBody.Messages[1])
	}
}

func TestInfer_NoAuthHeaderWhenKeyUnset(t *testing.T) {
	var sawAuth bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, sawAuth = r.Header["Authorization"]
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, sampleCompletion)
	}))
	defer srv.Close()

	b := newMockBackend(srv, "", "phi4-mini") // no API key -> local Ollama path
	if _, err := b.Infer(context.Background(), llm.Prompt{System: "s", User: "u"}); err != nil {
		t.Fatalf("Infer error: %v", err)
	}
	if sawAuth {
		t.Error("Authorization header sent despite empty API key; want none")
	}
}

func TestInfer_Degradation(t *testing.T) {
	t.Run("non-2xx status", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = io.WriteString(w, "boom")
		}))
		defer srv.Close()
		b := newMockBackend(srv, "", "m")
		out, err := b.Infer(context.Background(), llm.Prompt{})
		if err == nil {
			t.Fatal("want error on 500, got nil")
		}
		if out != "" {
			t.Errorf("want empty output on error, got %q", out)
		}
		if !strings.Contains(err.Error(), "500") {
			t.Errorf("error should mention status 500: %v", err)
		}
	})

	t.Run("connection refused", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
		client := srv.Client()
		srv.Close() // close immediately so the address refuses connections
		b := New(srv.URL+"/v1", "", "m", WithHTTPClient(client))
		if _, err := b.Infer(context.Background(), llm.Prompt{}); err == nil {
			t.Fatal("want error on connection refused, got nil")
		}
	})

	t.Run("timeout via ctx", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			time.Sleep(200 * time.Millisecond)
			_, _ = io.WriteString(w, sampleCompletion)
		}))
		defer srv.Close()
		b := newMockBackend(srv, "", "m")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
		defer cancel()
		if _, err := b.Infer(ctx, llm.Prompt{}); err == nil {
			t.Fatal("want error on ctx timeout, got nil")
		}
	})

	t.Run("malformed body", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, "not json {{{")
		}))
		defer srv.Close()
		b := newMockBackend(srv, "", "m")
		if _, err := b.Infer(context.Background(), llm.Prompt{}); err == nil {
			t.Fatal("want error on malformed body, got nil")
		}
	})

	t.Run("empty completion (no choices)", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"choices":[]}`)
		}))
		defer srv.Close()
		b := newMockBackend(srv, "", "m")
		_, err := b.Infer(context.Background(), llm.Prompt{})
		if !errors.Is(err, errEmptyCompletion) {
			t.Fatalf("want errEmptyCompletion, got %v", err)
		}
	})

	t.Run("error envelope with 200", func(t *testing.T) {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"error":{"message":"model not found"}}`)
		}))
		defer srv.Close()
		b := newMockBackend(srv, "", "m")
		_, err := b.Infer(context.Background(), llm.Prompt{})
		if err == nil || !strings.Contains(err.Error(), "model not found") {
			t.Fatalf("want error envelope surfaced, got %v", err)
		}
	})
}

func TestNew_Defaults(t *testing.T) {
	b := New("", "", "")
	if b.baseURL != DefaultBaseURL {
		t.Errorf("baseURL = %q, want default %q", b.baseURL, DefaultBaseURL)
	}
	if b.model != DefaultModel {
		t.Errorf("model = %q, want default %q", b.model, DefaultModel)
	}
	if b.httpClient == nil || b.httpClient.Timeout != DefaultTimeout {
		t.Errorf("httpClient timeout = %v, want %v", b.httpClient.Timeout, DefaultTimeout)
	}
}

func TestNew_TrimsTrailingSlash(t *testing.T) {
	b := New("http://x/v1/", "", "m")
	if b.baseURL != "http://x/v1" {
		t.Errorf("baseURL = %q, want trailing slash trimmed", b.baseURL)
	}
}

// TestWithTimeout covers the #1079 configurable HTTP timeout: a positive duration
// overrides DefaultTimeout, while a non-positive (or zero) duration is ignored so
// DefaultTimeout stands — mirroring the "non-positive = ignore" env semantics.
func TestWithTimeout(t *testing.T) {
	t.Run("positive overrides default", func(t *testing.T) {
		b := New("", "", "", WithTimeout(180*time.Second))
		if b.httpClient.Timeout != 180*time.Second {
			t.Errorf("timeout = %v, want 180s", b.httpClient.Timeout)
		}
	})
	t.Run("zero keeps default", func(t *testing.T) {
		b := New("", "", "", WithTimeout(0))
		if b.httpClient.Timeout != DefaultTimeout {
			t.Errorf("timeout = %v, want default %v", b.httpClient.Timeout, DefaultTimeout)
		}
	})
	t.Run("negative keeps default", func(t *testing.T) {
		b := New("", "", "", WithTimeout(-5*time.Second))
		if b.httpClient.Timeout != DefaultTimeout {
			t.Errorf("timeout = %v, want default %v", b.httpClient.Timeout, DefaultTimeout)
		}
	})
}

// withTraceContextPropagator installs the W3C TraceContext propagator as the global
// for the duration of a test, restoring the previous one afterward. This mirrors the
// agent's real otel.go setup (a composite that includes propagation.TraceContext) so
// the #1096 injection path under test behaves exactly as it does in production.
func withTraceContextPropagator(t *testing.T) {
	t.Helper()
	prev := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(prev) })
}

// TestInfer_InjectsTraceparentWithinActiveSpan is the #1096 core assertion: when Infer
// is called with a ctx carrying an active (sampled) span — exactly what the sync
// llmDecide path does after starting agent.llm.infer — the outbound request MUST carry
// a W3C `traceparent` header whose trace-id matches the active span's trace-id. This is
// what lets the litellm gateway nest its provider-truth gen_ai span under the agent's
// span (one trace). Infer must still succeed normally.
func TestInfer_InjectsTraceparentWithinActiveSpan(t *testing.T) {
	withTraceContextPropagator(t)

	var gotTraceparent string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotTraceparent = r.Header.Get("traceparent")
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, sampleCompletion)
	}))
	defer srv.Close()

	// A real SDK tracer provider with AlwaysSample so the started span is recording and
	// sampled — only sampled spans produce a traceparent the downstream will honor.
	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
	defer func() { _ = tp.Shutdown(context.Background()) }()
	ctx, span := tp.Tracer("test").Start(context.Background(), "agent.llm.infer")
	defer span.End()
	wantTraceID := span.SpanContext().TraceID().String()

	b := newMockBackend(srv, "", "phi4-mini")
	if _, err := b.Infer(ctx, llm.Prompt{System: "s", User: "u"}); err != nil {
		t.Fatalf("Infer error: %v", err)
	}

	if gotTraceparent == "" {
		t.Fatal("no traceparent header sent despite active span; want one")
	}
	// W3C format: version-traceid-spanid-flags; assert the trace-id matches the active
	// span so the gateway span will nest under THIS trace, not an orphan.
	if !strings.Contains(gotTraceparent, wantTraceID) {
		t.Errorf("traceparent %q does not carry active trace-id %q", gotTraceparent, wantTraceID)
	}
}

// TestInfer_TraceparentExtractsAsRemoteParent is the #1205 (epic #1140) two-sided
// stitch contract: it is not enough that openai.go WRITES a traceparent — the header
// the agent puts on the wire must, when EXTRACTED by a W3C TraceContextTextMapPropagator
// (exactly what the litellm gateway does in get_traceparent_from_header /
// _get_span_context), yield a VALID, REMOTE span context that carries the agent's active
// trace-id. That is the precise condition under which litellm parents its "Received Proxy
// Server Request" SERVER span UNDER the agent's trace instead of starting a new ROOT (the
// verification #11 disjoint-trace symptom). This test stands in for the gateway extractor
// so the contract is guarded in agent CI, with no live stack.
func TestInfer_TraceparentExtractsAsRemoteParent(t *testing.T) {
	withTraceContextPropagator(t)

	// Capture the exact headers the production HTTP client puts on the wire.
	var wireHeaders http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		wireHeaders = r.Header.Clone()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, sampleCompletion)
	}))
	defer srv.Close()

	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(sdktrace.AlwaysSample()))
	defer func() { _ = tp.Shutdown(context.Background()) }()
	ctx, span := tp.Tracer("test").Start(context.Background(), "agent.llm.infer.call")
	defer span.End()
	wantTraceID := span.SpanContext().TraceID().String()

	b := newMockBackend(srv, "", "phi4-mini")
	if _, err := b.Infer(ctx, llm.Prompt{System: "s", User: "u"}); err != nil {
		t.Fatalf("Infer error: %v", err)
	}
	if wireHeaders == nil {
		t.Fatal("server never observed the request headers")
	}

	// Replay the gateway's extraction over the wire headers. The agent must have sent a
	// usable header under one of the case-insensitive W3C names (Go canonicalizes to
	// "Traceparent"; litellm reads case-insensitively via Starlette). HeaderCarrier.Get
	// is canonicalizing too, so this mirrors a correct extractor.
	extracted := propagation.TraceContext{}.Extract(
		context.Background(), propagation.HeaderCarrier(wireHeaders),
	)
	remoteSC := trace.SpanContextFromContext(extracted)

	if !remoteSC.IsValid() {
		t.Fatalf("gateway-side Extract yielded no valid parent from headers %v; "+
			"litellm would start a ROOT span (the #1205 disjoint-trace break)", wireHeaders)
	}
	if !remoteSC.IsRemote() {
		t.Error("extracted parent is not marked Remote; litellm would not treat it as an inbound remote parent")
	}
	if got := remoteSC.TraceID().String(); got != wantTraceID {
		t.Errorf("extracted remote trace-id = %q, want the agent's active trace-id %q "+
			"(the gateway span would land in a DIFFERENT trace)", got, wantTraceID)
	}
	if !remoteSC.IsSampled() {
		t.Error("extracted parent is not sampled; the gateway child span may be dropped, orphaning the rich gen_ai error data")
	}
}

// TestInfer_NoTraceparentWithoutActiveSpan asserts the default-safe path: with no active
// span in ctx, Inject is a no-op (no traceparent header written) and Infer still works.
// This covers the Ollama-direct current setup (it ignores the header anyway) and any
// caller that runs outside a span — the injection must never break a plain call.
func TestInfer_NoTraceparentWithoutActiveSpan(t *testing.T) {
	withTraceContextPropagator(t)

	var sawTraceparent bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, sawTraceparent = r.Header["Traceparent"]
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, sampleCompletion)
	}))
	defer srv.Close()

	b := newMockBackend(srv, "", "phi4-mini")
	// context.Background() carries no span -> propagator Inject writes nothing.
	out, err := b.Infer(context.Background(), llm.Prompt{System: "s", User: "u"})
	if err != nil {
		t.Fatalf("Infer error with no active span: %v", err)
	}
	if out == "" {
		t.Error("want content returned even with no active span")
	}
	if sawTraceparent {
		t.Error("traceparent header sent without an active span; want none (no-op inject)")
	}
}
