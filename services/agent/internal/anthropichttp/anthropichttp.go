// Package anthropichttp is the one place the agent builds HTTP clients for
// key-bearing requests and the Anthropic SDK client (UC-044 BR-003, C-016).
// net/http strips only Authorization and Cookie across hosts, so an x-api-key
// or a custom auth header would reach a redirect target; every client built
// here returns a redirect as the response instead of following it.
package anthropichttp

import (
	"net/http"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// NoRedirect is an http.Client.CheckRedirect that never follows a redirect: the
// 3xx surfaces as the response (callers treat it as an error status).
func NoRedirect(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }

// NewHTTPClient returns a client that never follows a redirect. A zero timeout
// means no client-wide timeout (the caller bounds each request with a ctx).
func NewHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, CheckRedirect: NoRedirect}
}

// NewClient builds the Anthropic SDK client for apiKey at baseURL over httpClient.
// Credentials and base URL come from the arguments only (no ANTHROPIC_* env
// variables, profile files or federation middleware), retries are off (callers
// degrade on error), the SDK's own OTel is off (the caller owns the single chat
// span) and W3C traceparent is injected by middleware instead, so a proxy used
// as baseURL can nest its spans under ours. The Anthropic API ignores it.
func NewClient(apiKey, baseURL string, httpClient *http.Client) anthropic.Client {
	return anthropic.NewClient(
		option.WithoutEnvironmentDefaults(),
		option.WithAPIKey(apiKey),
		option.WithBaseURL(baseURL),
		option.WithHTTPClient(httpClient),
		option.WithMaxRetries(0),
		option.WithoutOpenTelemetry(),
		option.WithMiddleware(traceContext),
	)
}

func traceContext(req *http.Request, next option.MiddlewareNext) (*http.Response, error) {
	otel.GetTextMapPropagator().Inject(req.Context(), propagation.HeaderCarrier(req.Header))
	return next(req)
}
