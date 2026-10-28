package anthropichttp

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/anthropics/anthropic-sdk-go"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// TestNewClient_NeverFollowsRedirect: whatever http.Client the caller passes
// (nil, a default one that follows redirects, one with its own CheckRedirect),
// the SDK client returns the 3xx as an error and the key never reaches the
// redirect target. The caller's client is left unmodified (#2321).
// UC: UC-044 BR-003
func TestNewClient_NeverFollowsRedirect(t *testing.T) {
	follow := func(*http.Request, []*http.Request) error { return nil }
	clients := map[string]func() *http.Client{
		"nil":            func() *http.Client { return nil },
		"default client": func() *http.Client { return &http.Client{} },
		"explicit follow": func() *http.Client {
			return &http.Client{CheckRedirect: follow}
		},
	}
	for name, mk := range clients {
		t.Run(name, func(t *testing.T) {
			var targetHits atomic.Int32
			target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				targetHits.Add(1)
			}))
			t.Cleanup(target.Close)
			redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				http.Redirect(w, r, target.URL+r.URL.Path, http.StatusTemporaryRedirect)
			}))
			t.Cleanup(redirector.Close)

			hc := mk()
			c := NewClient("sk-redirect-secret", redirector.URL, hc)
			_, err := c.Messages.New(context.Background(), anthropic.MessageNewParams{
				Model:     "claude-haiku-4-5",
				MaxTokens: 1,
				Messages:  []anthropic.MessageParam{anthropic.NewUserMessage(anthropic.NewTextBlock("u"))},
			})
			if err == nil {
				t.Error("request succeeded through a redirect, want an error")
			}
			if n := targetHits.Load(); n != 0 {
				t.Errorf("redirect target received %d request(s) (x-api-key leaked)", n)
			}
			if hc != nil && hc.CheckRedirect != nil && hc.CheckRedirect(nil, nil) != nil {
				t.Error("the caller's http.Client was modified")
			}
		})
	}
}

func TestTraceContextMiddleware_InjectsTraceparent(t *testing.T) {
	prev := otel.GetTextMapPropagator()
	otel.SetTextMapPropagator(propagation.TraceContext{})
	t.Cleanup(func() { otel.SetTextMapPropagator(prev) })
	ctx, span := sdktrace.NewTracerProvider().Tracer("t").Start(context.Background(), "chat")
	defer span.End()

	req := httptest.NewRequest(http.MethodPost, "http://x/v1/messages", nil).WithContext(ctx)
	var got string
	_, _ = TraceContextMiddleware(req, func(r *http.Request) (*http.Response, error) {
		got = r.Header.Get("traceparent")
		return nil, nil
	})
	if got == "" {
		t.Error("traceparent not injected")
	}
}

func TestStripDateSuffix(t *testing.T) {
	for in, want := range map[string]string{
		"claude-haiku-4-5-20251001": "claude-haiku-4-5",
		"claude-haiku-4-5":          "claude-haiku-4-5",
		"claude-x-2025100a":         "claude-x-2025100a",
		"claude-x-1234567":          "claude-x-1234567",
		"nodash":                    "nodash",
	} {
		if got := StripDateSuffix(in); got != want {
			t.Errorf("StripDateSuffix(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestServerAddress(t *testing.T) {
	cases := []struct {
		in   string
		host string
		port int
	}{
		{"https://api.anthropic.com", "api.anthropic.com", 443},
		{"http://proxy.local/anthropic/", "proxy.local", 80},
		{"http://127.0.0.1:8080", "127.0.0.1", 8080},
		{"::bad", "", 0},
	}
	for _, c := range cases {
		if h, p := ServerAddress(c.in); h != c.host || p != c.port {
			t.Errorf("ServerAddress(%q) = %s:%d, want %s:%d", c.in, h, p, c.host, c.port)
		}
	}
}

func TestSystemSHA(t *testing.T) {
	// sha256("") = e3b0c44298fc1c149afbf4c8996fb924...
	if got := SystemSHA(""); got != "e3b0c44298fc1c14" {
		t.Errorf("SystemSHA(\"\") = %q", got)
	}
}

func TestErrorType(t *testing.T) {
	ctx := context.Background()
	expired, cancel := context.WithTimeout(ctx, 0)
	defer cancel()
	for name, c := range map[string]struct {
		ctx  context.Context
		err  error
		want string
	}{
		"api error":   {ctx, &anthropic.Error{StatusCode: 529}, "529"},
		"deadline":    {ctx, context.DeadlineExceeded, "timeout"},
		"ctx expired": {expired, errors.New("x"), "timeout"},
		"canceled":    {ctx, context.Canceled, "canceled"},
		"other":       {ctx, errors.New("boom"), "_OTHER"},
	} {
		if got := ErrorType(c.ctx, c.err); got != c.want {
			t.Errorf("%s: ErrorType = %q, want %q", name, got, c.want)
		}
	}
}
