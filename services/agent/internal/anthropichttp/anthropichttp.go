// Package anthropichttp is the plumbing shared by the agent's two Anthropic SDK
// clients: the claude decision backend (decide/backends/claude, Messages API)
// and the offline inference backend (inference, Beta Messages API). It builds
// the SDK client and holds the span helpers both record, so the two stay
// identical. Each caller keeps its own API surface, request shape and span.
//
// It lives under the module's internal/ (not decide/backends/internal/) so that
// package inference can import it too.
package anthropichttp

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
)

// AttrRequestReasoningLevel is gen_ai.request.reasoning.level, which postdates
// the vendored semconv v1.41.0 (no Go constant yet). The name matches what
// anthropic-sdk-go's own instrumentation records for output_config.effort.
const AttrRequestReasoningLevel = attribute.Key("gen_ai.request.reasoning.level")

// AttrSystemSHA256 fingerprints the system prompt on every span (SystemSHA),
// mirroring llm.prompt.system_sha256 in decision/prompt_fingerprint.go. The
// full text stays off the span unless content capture is on: stamping multi-KB
// prompts on every span pushed the collector into OOM in #1167.
const AttrSystemSHA256 = attribute.Key("llm.prompt.system_sha256")

const systemSHALen = 16

// NewClient builds an Anthropic SDK client that sends apiKey to baseURL only.
//
//   - Credentials and base URL come from the arguments only: no ANTHROPIC_* env
//     variables, profile files, env auth token or federation middleware.
//   - It never follows a redirect, whatever httpClient does: net/http strips only
//     Authorization and Cookie across hosts, so x-api-key would reach the
//     redirect target. The Anthropic API does not redirect; a 3xx surfaces as an
//     error. httpClient itself is not modified; its transport (and so the pooled
//     connections) is shared.
//   - SDK retries are off: the callers degrade on error (the rules backend, or no
//     proposal / no conclusion this time).
//   - The SDK's built-in GenAI instrumentation is off: the caller owns the single
//     chat span. That also turns off the SDK's traceparent header, which
//     TraceContextMiddleware sends instead.
func NewClient(apiKey, baseURL string, httpClient *http.Client) anthropic.Client {
	return anthropic.NewClient(
		option.WithoutEnvironmentDefaults(),
		option.WithAPIKey(apiKey),
		option.WithBaseURL(baseURL),
		option.WithHTTPClient(noRedirect(httpClient)),
		option.WithMaxRetries(0),
		option.WithoutOpenTelemetry(),
		option.WithMiddleware(TraceContextMiddleware),
	)
}

// noRedirect returns a shallow copy of c (a new client when c is nil) whose
// CheckRedirect returns the 3xx as the response instead of following it.
func noRedirect(c *http.Client) *http.Client {
	out := &http.Client{}
	if c != nil {
		*out = *c
	}
	out.CheckRedirect = NoRedirect
	return out
}

// NoRedirect is the one http.Client.CheckRedirect that never follows a
// redirect: the 3xx surfaces as the response. net/http strips only
// Authorization and Cookie across hosts, so an x-api-key header would reach a
// redirect target (UC-044 BR-003, C-016).
func NoRedirect(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

// NewHTTPClient returns a plain HTTP client that never follows a redirect, for
// key-bearing requests that do not go through the SDK. A zero timeout means no
// client-wide timeout.
func NewHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, CheckRedirect: NoRedirect}
}

// TraceContextMiddleware injects W3C traceparent from the request ctx (the chat
// span), so a proxy configured as the base URL can nest its spans under ours.
// The Anthropic API ignores it.
func TraceContextMiddleware(req *http.Request, next option.MiddlewareNext) (*http.Response, error) {
	otel.GetTextMapPropagator().Inject(req.Context(), propagation.HeaderCarrier(req.Header))
	return next(req)
}

// SystemSHA is the first 16 hex chars of the sha256 of the system prompt.
func SystemSHA(system string) string {
	sum := sha256.Sum256([]byte(system))
	return hex.EncodeToString(sum[:])[:systemSHALen]
}

// StripDateSuffix removes a trailing "-YYYYMMDD" snapshot date.
func StripDateSuffix(model string) string {
	i := strings.LastIndexByte(model, '-')
	if i < 0 || len(model)-i-1 != 8 {
		return model
	}
	for _, c := range model[i+1:] {
		if c < '0' || c > '9' {
			return model
		}
	}
	return model[:i]
}

// ErrorType is the low-cardinality error.type of a failed SDK call: the HTTP
// status code for API errors (the convention for HTTP clients), "timeout",
// "canceled", or "_OTHER".
func ErrorType(ctx context.Context, err error) string {
	var apiErr *anthropic.Error
	switch {
	case errors.As(err, &apiErr):
		return strconv.Itoa(apiErr.StatusCode)
	case errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded):
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "timeout"
	}
	return semconv.ErrorTypeOther.Value.AsString()
}

// ServerAddress splits a base URL into server.address / server.port.
func ServerAddress(base string) (string, int) {
	u, err := url.Parse(base)
	if err != nil || u.Hostname() == "" {
		return "", 0
	}
	if p := u.Port(); p != "" {
		if n, err := strconv.Atoi(p); err == nil {
			return u.Hostname(), n
		}
	}
	if u.Scheme == "http" {
		return u.Hostname(), 80
	}
	return u.Hostname(), 443
}
