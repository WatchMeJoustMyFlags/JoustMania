// Package claude is the "claude" decision backend of epic #1225 (#1227): a
// direct Anthropic Messages API call, no gateway, that answers the typed decide
// questions with structured output (a JSON schema via output_config.format).
//
// It is pure: it reads no flags and does not import package decision. The
// caller (decision wiring, #1226) builds a Config from flags on every call, so
// the model and timeout switch live, and runs decide.Validate on the Response
// (Decide also validates, via llmio.Parse).
//
// SDK choice: the official Go SDK (github.com/anthropics/anthropic-sdk-go). It
// is pure Go and CGO-free, so the agent stays a static binary, and it gives the
// typed request surface this call needs (output_config.format, the per-model
// thinking/effort union, typed usage and stop reasons, typed API errors). The
// SDK's own retries are turned OFF: inside a ~2.5s in-game budget a retry after
// a 429/5xx would only stall; the rules backend is the retry.
//
// Latency: the in-game budget is ~2.5s, so extended thinking stays off wherever
// the model allows it and effort is "low" where it does not (see shapeFor). The
// "why" comes from the short rationale field of the structured answer instead.
// If the model nevertheless returns thinking text, it is recorded as a GenAI
// "reasoning" output part.
package claude

import (
	"context"
	"errors"
	"fmt"
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
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
)

// Name is the backend name (the decision.backend flag value).
const Name = "claude"

const (
	// DefaultBaseURL is the Anthropic API root.
	DefaultBaseURL = "https://api.anthropic.com"
	// DefaultTimeout is the per-call budget when Config.Timeout is not positive.
	DefaultTimeout = 2500 * time.Millisecond
	// MaxTokens bounds the answer. A full FFA answer is ~150-300 output tokens;
	// the headroom covers low-effort thinking on models where it cannot be
	// turned off. Hitting it is an error (truncated JSON), never a partial act.
	MaxTokens = 2048
)

// instrumentationName scopes the tracer and meter.
const instrumentationName = "github.com/joustmania/agent/decide/backends/claude"

// Config configures one Backend. It is built by the caller from flags (model,
// timeout, capture) and env (APIKey) on every decision.
type Config struct {
	// APIKey is the Anthropic API key (ANTHROPIC_API_KEY). Empty = unavailable.
	APIKey string
	// BaseURL overrides DefaultBaseURL (tests, or a proxy).
	BaseURL string
	// Model is the model id, e.g. "claude-haiku-4-5". Empty = unavailable.
	Model string
	// Timeout bounds one Decide call; not positive means DefaultTimeout.
	Timeout time.Duration
	// HTTPClient overrides the shared default client. Leave nil in production:
	// the default is shared across New calls so the TLS connection to the API
	// is reused, which matters at a 2.5s budget.
	HTTPClient *http.Client
	// CaptureContent records the prompt and model output on the span
	// (gen_ai.system_instructions / gen_ai.input.messages /
	// gen_ai.output.messages). Off by default: content is opt-in per semconv.
	CaptureContent bool
}

// Backend is the claude decision backend. It implements decide.Backend.
type Backend struct {
	cfg  Config
	host string
	port int
}

// defaultHTTPClient is shared by every Backend without a Config.HTTPClient so
// connections are pooled across the per-decision New calls. Its own timeout is
// a backstop; Decide's ctx deadline is the real budget.
var defaultHTTPClient = &http.Client{Timeout: 30 * time.Second}

// New builds a Backend. It never fails; an incomplete Config makes Available
// false and Decide return an error.
func New(cfg Config) *Backend {
	cfg.BaseURL = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	if cfg.BaseURL == "" {
		cfg.BaseURL = DefaultBaseURL
	}
	cfg.Model = strings.TrimSpace(cfg.Model)
	if cfg.Timeout <= 0 {
		cfg.Timeout = DefaultTimeout
	}
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = defaultHTTPClient
	}
	host, port := serverAddress(cfg.BaseURL)
	return &Backend{cfg: cfg, host: host, port: port}
}

// Name implements decide.Backend.
func (b *Backend) Name() string { return Name }

// Available reports whether the backend is configured (an API key and a model).
func (b *Backend) Available() bool {
	return b != nil && strings.TrimSpace(b.cfg.APIKey) != "" && b.cfg.Model != ""
}

// ErrUnavailable is returned by Decide when Available is false.
var ErrUnavailable = errors.New("claude: backend unavailable (no API key or model)")

// Decide asks the model to answer req.Questions about req.State. It reads only
// req.State and req.Questions. Every failure (unavailable, timeout, HTTP error,
// refusal, truncation, invalid answer) is a clean error; it never panics.
// Invalid answers wrap decide.ErrInvalid.
func (b *Backend) Decide(ctx context.Context, req decide.Request) (decide.Response, error) {
	if !b.Available() {
		return decide.Response{}, ErrUnavailable
	}
	if len(req.Questions) == 0 {
		return decide.Response{}, fmt.Errorf("%w: no questions", decide.ErrInvalid)
	}

	ctx, cancel := context.WithTimeout(ctx, b.cfg.Timeout)
	defer cancel()

	prompt := llmio.Render(req)
	shape := shapeFor(b.cfg.Model)
	params := b.params(prompt, shape)

	ctx, span := otel.Tracer(instrumentationName).Start(ctx,
		"chat "+b.cfg.Model,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(b.requestAttributes(shape)...),
	)
	defer span.End()
	if b.cfg.CaptureContent {
		span.SetAttributes(llmio.InputContentAttributes(prompt)...)
	}

	start := time.Now()
	client := b.client()
	msg, err := client.Messages.New(ctx, params)
	call := callResult{msg: msg, start: start}

	if err != nil {
		call.errType = errorType(ctx, err)
		b.finish(ctx, span, call, nil)
		return decide.Response{}, fmt.Errorf("claude: %s: %w", call.errType, err)
	}

	text, thinking := contentOf(msg)
	call.text, call.thinking = text, thinking

	switch msg.StopReason {
	case anthropic.StopReasonEndTurn, anthropic.StopReasonStopSequence:
	case anthropic.StopReasonRefusal:
		call.errType = "refusal"
	case anthropic.StopReasonMaxTokens:
		call.errType = "max_tokens"
	default:
		call.errType = "stop_reason_" + string(msg.StopReason)
	}
	if call.errType != "" {
		b.finish(ctx, span, call, nil)
		return decide.Response{}, fmt.Errorf("%w: claude: stop_reason %s", decide.ErrInvalid, msg.StopReason)
	}

	resp, perr := llmio.Parse(req, []byte(text), Name)
	if perr != nil {
		call.errType = "invalid_output"
		b.finish(ctx, span, call, nil)
		return decide.Response{}, fmt.Errorf("claude: %w", perr)
	}
	b.finish(ctx, span, call, &resp)
	return resp, nil
}

// client builds the SDK client. It is cheap (an options slice); the pooled
// connections live in the http.Client.
func (b *Backend) client() anthropic.Client {
	return anthropic.NewClient(
		option.WithAPIKey(b.cfg.APIKey),
		option.WithBaseURL(b.cfg.BaseURL),
		option.WithHTTPClient(b.cfg.HTTPClient),
		option.WithMaxRetries(0),
		// The SDK has built-in GenAI instrumentation. It is off here: Decide owns
		// the single chat span (one GenAI span per call, not two), because the
		// outcome that matters (parse/validation, rationale as a reasoning part)
		// is only known after the SDK returns. Turning SDK traces off also turns
		// off its traceparent header, which the middleware below sends instead.
		option.WithoutOpenTelemetry(),
		option.WithMiddleware(traceContextMiddleware),
	)
}

// traceContextMiddleware injects W3C traceparent from the request ctx (the
// chat span), as inference/openai.go does, so a proxy or gateway configured as
// BaseURL can nest its spans under ours. The Anthropic API ignores it.
func traceContextMiddleware(req *http.Request, next option.MiddlewareNext) (*http.Response, error) {
	otel.GetTextMapPropagator().Inject(req.Context(), propagation.HeaderCarrier(req.Header))
	return next(req)
}

// attrRequestReasoningLevel is gen_ai.request.reasoning.level, which postdates
// the vendored semconv v1.41.0 (no Go constant yet). The name matches what
// anthropic-sdk-go's own instrumentation records for output_config.effort.
const attrRequestReasoningLevel = attribute.Key("gen_ai.request.reasoning.level")

// requestShape is the per-model latency tuning: thinking off where the model
// allows it, low effort where it does not, temperature 0 where sampling
// parameters are accepted.
type requestShape struct {
	thinking    anthropic.ThinkingConfigParamUnion
	effort      anthropic.OutputConfigEffort
	temperature *float64
}

// shapeFor picks the request shape for model. Order matters ("claude-sonnet-5"
// is a prefix of "claude-sonnet-5-5"). Unknown models get no extra fields, so a
// new model never 400s on a parameter it does not take.
func shapeFor(model string) requestShape {
	zero := 0.0
	switch {
	case strings.HasPrefix(model, "claude-haiku-4-5"):
		// No thinking unless asked for; sampling parameters accepted; effort is not.
		return requestShape{temperature: &zero}
	case strings.HasPrefix(model, "claude-sonnet-5-5"):
		// "disabled" is a 400 here; between_tools turns thinking off for a
		// tool-less call and is accepted at effort high or below.
		return requestShape{
			thinking: anthropic.ThinkingConfigParamUnion{OfBetweenTools: &anthropic.ThinkingConfigBetweenToolsParam{}},
			effort:   anthropic.OutputConfigEffortLow,
		}
	case strings.HasPrefix(model, "claude-opus-5-5"), strings.HasPrefix(model, "claude-fable-5"):
		// Thinking cannot be disabled; low effort keeps it short.
		return requestShape{effort: anthropic.OutputConfigEffortLow}
	case strings.HasPrefix(model, "claude-opus-5"), strings.HasPrefix(model, "claude-sonnet-5"):
		// Thinking on by default; disabled is accepted at effort high or below.
		return requestShape{
			thinking: anthropic.ThinkingConfigParamUnion{OfDisabled: &anthropic.ThinkingConfigDisabledParam{}},
			effort:   anthropic.OutputConfigEffortLow,
		}
	default:
		return requestShape{}
	}
}

func (b *Backend) params(p llmio.Prompt, shape requestShape) anthropic.MessageNewParams {
	params := anthropic.MessageNewParams{
		Model:     anthropic.Model(b.cfg.Model),
		MaxTokens: MaxTokens,
		System:    []anthropic.TextBlockParam{{Text: p.System}},
		Messages: []anthropic.MessageParam{
			anthropic.NewUserMessage(anthropic.NewTextBlock(p.User)),
		},
		OutputConfig: anthropic.OutputConfigParam{
			Effort: shape.effort,
			Format: anthropic.JSONOutputFormatParam{Schema: llmio.AnswerSchema()},
		},
		Thinking: shape.thinking,
	}
	if shape.temperature != nil {
		params.Temperature = anthropic.Float(*shape.temperature)
	}
	return params
}

func (b *Backend) requestAttributes(shape requestShape) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameAnthropic,
		semconv.GenAIRequestModel(b.cfg.Model),
		semconv.GenAIRequestMaxTokens(MaxTokens),
		semconv.GenAIOutputTypeJSON,
		semconv.ServerAddress(b.host),
		semconv.ServerPort(b.port),
	}
	if shape.temperature != nil {
		attrs = append(attrs, semconv.GenAIRequestTemperature(*shape.temperature))
	}
	if shape.effort != "" {
		attrs = append(attrs, attrRequestReasoningLevel.String(string(shape.effort)))
	}
	return attrs
}

// callResult is what finish records on the span and metrics.
type callResult struct {
	msg      *anthropic.Message
	start    time.Time
	text     string
	thinking []string
	errType  string
}

// finish records the response attributes, content (when captured), error and
// GenAI client metrics.
func (b *Backend) finish(ctx context.Context, span trace.Span, c callResult, resp *decide.Response) {
	duration := time.Since(c.start)
	responseModel := ""
	if c.msg != nil {
		m := c.msg
		responseModel = string(m.Model)
		input := int(m.Usage.InputTokens + m.Usage.CacheReadInputTokens + m.Usage.CacheCreationInputTokens)
		span.SetAttributes(
			semconv.GenAIResponseID(m.ID),
			semconv.GenAIResponseModel(responseModel),
			semconv.GenAIResponseFinishReasons(string(m.StopReason)),
			// Per the semconv Anthropic page, input_tokens includes cached tokens.
			semconv.GenAIUsageInputTokens(input),
			semconv.GenAIUsageOutputTokens(int(m.Usage.OutputTokens)),
			semconv.GenAIUsageCacheReadInputTokens(int(m.Usage.CacheReadInputTokens)),
			semconv.GenAIUsageCacheCreationInputTokens(int(m.Usage.CacheCreationInputTokens)),
		)
		if r := m.Usage.OutputTokensDetails.ThinkingTokens; r > 0 {
			span.SetAttributes(semconv.GenAIUsageReasoningOutputTokens(int(r)))
		}
		if b.cfg.CaptureContent {
			rationale := ""
			if resp != nil {
				rationale = resp.Rationale
			}
			span.SetAttributes(llmio.OutputContentAttribute(
				llmio.OutputMessage(c.text, c.thinking, rationale, finishReason(m.StopReason))))
		}
		recordTokens(ctx, b, responseModel, input, int(m.Usage.OutputTokens))
	}
	if c.errType != "" {
		span.SetAttributes(semconv.ErrorTypeKey.String(c.errType))
		span.SetStatus(codes.Error, c.errType)
	}
	recordDuration(ctx, b, responseModel, c.errType, duration)
}

// contentOf concatenates the text blocks and collects non-empty thinking text.
func contentOf(m *anthropic.Message) (string, []string) {
	var text strings.Builder
	var thinking []string
	for _, block := range m.Content {
		switch v := block.AsAny().(type) {
		case anthropic.TextBlock:
			text.WriteString(v.Text)
		case anthropic.ThinkingBlock:
			if strings.TrimSpace(v.Thinking) != "" {
				thinking = append(thinking, v.Thinking)
			}
		}
	}
	return text.String(), thinking
}

// finishReason maps an Anthropic stop_reason onto the semconv output-message
// finish_reason enum.
func finishReason(r anthropic.StopReason) string {
	switch r {
	case anthropic.StopReasonEndTurn, anthropic.StopReasonStopSequence:
		return llmio.FinishStop
	case anthropic.StopReasonMaxTokens, anthropic.StopReasonModelContextWindowExceeded:
		return llmio.FinishLength
	case anthropic.StopReasonRefusal:
		return llmio.FinishContentFilter
	case anthropic.StopReasonToolUse:
		return llmio.FinishToolCall
	default:
		return string(r)
	}
}

// errorType is the low-cardinality error.type of a failed call: "timeout",
// "canceled", the HTTP status code for API errors (the convention for HTTP
// clients), or "_OTHER".
func errorType(ctx context.Context, err error) string {
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

// serverAddress splits a base URL into server.address / server.port.
func serverAddress(base string) (string, int) {
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
