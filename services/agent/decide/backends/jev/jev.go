// Package jev is the "jev" decision backend of epic #1225 (#1229): TypeSafe's
// hosted Jev API, called directly (no gateway).
//
// STATUS: STUB. We have neither an API key nor API docs yet. Available() is
// false until both Config.APIKey (JEV_API_KEY) and Config.BaseURL
// (decision.jev.base_url) are set, so with decision.backend=jev the decision
// wiring falls back to rules with fallback_reason=unavailable. The wire format
// is isolated in wire.go and is currently an ASSUMED OpenAI-compatible shape;
// see wire.go and README.md for what is needed from Jev.
//
// It is pure: it reads no flags and does not import package decision. The
// caller (decision wiring, #1226) builds a Config from flags
// (decision.jev.base_url / .model / .timeout_ms, decision.capture_content) and
// env (JEV_API_KEY) on every call.
//
// Telemetry mirrors the claude and openai backends: one CLIENT span per call
// named "chat {model}" (just "chat" without a model) with the OTel GenAI
// semantic conventions (v1.41.0) and gen_ai.provider.name=jev (a custom value;
// semconv has no well-known one), opt-in content capture, and the GenAI client
// metrics.
package jev

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/backends/internal/chathttp"
	"github.com/joustmania/agent/decide/llmio"
)

// Name is the backend name (the decision.backend flag value).
const Name = "jev"

// ProviderName is the gen_ai.provider.name of Jev calls (custom value).
const ProviderName = "jev"

// DefaultTimeout is the per-call budget when Config.Timeout is not positive.
const DefaultTimeout = 2500 * time.Millisecond

// instrumentationName scopes the tracer and meter.
const instrumentationName = "github.com/joustmania/agent/decide/backends/jev"

var metrics = chathttp.NewMetrics(instrumentationName)

// Config configures one Backend. Same shape as the claude and openai backends.
type Config struct {
	// APIKey is the Jev API key (JEV_API_KEY). Empty = unavailable.
	APIKey string
	// BaseURL is the Jev API root (decision.jev.base_url). Empty = unavailable.
	BaseURL string
	// Model is the Jev model id, if Jev has model selection
	// (decision.jev.model). Optional.
	Model string
	// Timeout bounds one Decide call; not positive means DefaultTimeout.
	Timeout time.Duration
	// HTTPClient overrides the shared default client. Leave nil in production.
	HTTPClient *http.Client
	// CaptureContent records the request and model output on the span. Off by
	// default: content is opt-in per semconv.
	CaptureContent bool
}

// Backend is the jev decision backend. It implements decide.Backend.
type Backend struct {
	cfg  Config
	host string
	port int
}

// New builds a Backend. It never fails; an incomplete Config makes Available
// false and Decide return ErrUnavailable.
func New(cfg Config) *Backend {
	cfg.BaseURL = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	cfg.Model = strings.TrimSpace(cfg.Model)
	if cfg.Timeout <= 0 {
		cfg.Timeout = DefaultTimeout
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = chathttp.DefaultHTTPClient
	}
	host, port := chathttp.ServerAddress(cfg.BaseURL)
	return &Backend{cfg: cfg, host: host, port: port}
}

// Name implements decide.Backend.
func (b *Backend) Name() string { return Name }

// Available reports whether the backend is configured: an API key and a base
// URL. Without them the caller falls back to rules (fallback_reason
// unavailable).
func (b *Backend) Available() bool {
	return b != nil && strings.TrimSpace(b.cfg.APIKey) != "" && b.cfg.BaseURL != ""
}

// ErrUnavailable is returned by Decide when Available is false.
var ErrUnavailable = errors.New("jev: backend unavailable (no JEV_API_KEY or base URL)")

// Decide asks Jev to answer req.Questions about req.State. It reads only
// req.State and req.Questions (wire.go's encodeRequest). Every failure is a
// clean error; it never panics. Invalid answers wrap decide.ErrInvalid.
func (b *Backend) Decide(ctx context.Context, req decide.Request) (decide.Response, error) {
	if !b.Available() {
		return decide.Response{}, ErrUnavailable
	}
	if len(req.Questions) == 0 {
		return decide.Response{}, fmt.Errorf("%w: no questions", decide.ErrInvalid)
	}

	ctx, cancel := context.WithTimeout(ctx, b.cfg.Timeout)
	defer cancel()

	out, err := encodeRequest(b.cfg.Model, req)
	if err != nil {
		return decide.Response{}, err
	}

	spanName := "chat"
	if b.cfg.Model != "" {
		spanName += " " + b.cfg.Model
	}
	ctx, span := otel.Tracer(instrumentationName).Start(ctx, spanName,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(b.requestAttributes(out)...),
	)
	defer span.End()
	if b.cfg.CaptureContent {
		span.SetAttributes(llmio.InputContentAttributes(out.Content)...)
	}

	start := time.Now()
	header := http.Header{}
	setAuth(header, b.cfg.APIKey)
	status, raw, err := chathttp.Post(ctx, b.cfg.HTTPClient, b.cfg.BaseURL+wirePath, header, out.Body, chathttp.MaxResponseBytes)

	switch {
	case errors.Is(err, chathttp.ErrTooLarge):
		b.finish(ctx, span, start, nil, "response_too_large")
		return decide.Response{}, fmt.Errorf("jev: %w: response body over %d bytes", llmio.ErrMalformed, chathttp.MaxResponseBytes)
	case err != nil:
		errType := chathttp.TransportErrorType(ctx, err)
		b.finish(ctx, span, start, nil, errType)
		return decide.Response{}, fmt.Errorf("jev: %s: %w", errType, err)
	case status < 200 || status > 299:
		b.finish(ctx, span, start, nil, chathttp.StatusErrorType(status))
		return decide.Response{}, fmt.Errorf("jev: status %d: %s", status, clip(string(raw), 256))
	}

	res, errType, err := decodeResponse(req, raw)
	b.finish(ctx, span, start, &res, errType)
	if err != nil {
		return decide.Response{}, err
	}
	return res.Response, nil
}

func (b *Backend) requestAttributes(out wireRequestOut) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameKey.String(ProviderName),
		semconv.GenAIOutputTypeJSON,
		semconv.ServerAddress(b.host),
		semconv.ServerPort(b.port),
	}
	if b.cfg.Model != "" {
		attrs = append(attrs, semconv.GenAIRequestModel(b.cfg.Model))
	}
	if out.MaxTokens > 0 {
		attrs = append(attrs, semconv.GenAIRequestMaxTokens(out.MaxTokens))
	}
	if out.Temperature != nil {
		attrs = append(attrs, semconv.GenAIRequestTemperature(*out.Temperature))
	}
	return attrs
}

// finish records the response attributes, content (when captured), error and
// GenAI client metrics. res is nil when no 2xx body was decoded.
func (b *Backend) finish(ctx context.Context, span trace.Span, start time.Time, res *wireResult, errType string) {
	duration := time.Since(start)
	mc := chathttp.Call{Provider: ProviderName, RequestModel: b.cfg.Model, Host: b.host, Port: b.port}
	if res != nil {
		mc.ResponseModel = res.Model
		if res.ID != "" {
			span.SetAttributes(semconv.GenAIResponseID(res.ID))
		}
		if res.Model != "" {
			span.SetAttributes(semconv.GenAIResponseModel(res.Model))
		}
		if len(res.FinishReasons) > 0 {
			span.SetAttributes(semconv.GenAIResponseFinishReasons(res.FinishReasons...))
		}
		if u := res.Usage; u != nil {
			span.SetAttributes(
				semconv.GenAIUsageInputTokens(u.InputTokens),
				semconv.GenAIUsageOutputTokens(u.OutputTokens),
			)
			if u.ReasoningTokens > 0 {
				span.SetAttributes(semconv.GenAIUsageReasoningOutputTokens(u.ReasoningTokens))
			}
			metrics.RecordTokens(ctx, mc, u.InputTokens, u.OutputTokens)
		}
		if b.cfg.CaptureContent && (res.Text != "" || len(res.Reasoning) > 0) {
			span.SetAttributes(llmio.OutputContentAttribute(
				llmio.OutputMessage(res.Text, res.Reasoning, res.Response.Rationale, res.OutputFinishReason)))
		}
	}
	if errType != "" {
		span.SetAttributes(semconv.ErrorTypeKey.String(errType))
		span.SetStatus(codes.Error, errType)
	}
	metrics.RecordDuration(ctx, mc, errType, duration)
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n] + "...(truncated)"
}
