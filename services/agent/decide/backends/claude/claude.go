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
// SDK's own retries are turned OFF: inside a few-second in-game budget a retry after
// a 429/5xx would only stall; the rules backend is the retry.
//
// Latency: the in-game budget is a few seconds (decision.claude.timeout_ms), so
// thinking stays off or minimal: off where the model allows it cleanly, and
// adaptive at "low" effort (which skips thinking on requests this simple)
// elsewhere (see shapeFor). The "why" comes from the short rationale field of
// the structured answer instead. If the model nevertheless returns thinking
// text, it is recorded as a GenAI "reasoning" output part.
//
// Prompt caching is deliberately NOT used (#1231). The static system prompt
// (~490-630 tokens, plus ~480 the API adds for the output schema) reaches the
// 512-token minimum only on Sonnet 5.5 / Opus 5.5 / Opus 5 (Haiku 4.5 needs
// 4096), and on Sonnet 5.5 a cached ~1.1k-token prefix measured no TTFT gain
// (cache reads verified, TTFT within the uncached spread); the cost saving is
// ~$0.002 per call.
package claude

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/internal/anthropichttp"
)

// Name is the backend name (the decision.backend flag value).
const Name = "claude"

const (
	// DefaultBaseURL is the Anthropic API root.
	DefaultBaseURL = "https://api.anthropic.com"
	// DefaultTimeout is the per-call budget when Config.Timeout is not positive.
	// It matches the decision.claude.timeout_ms flag default (#1231): measured
	// live, Haiku 4.5 and Sonnet 5.5 answer in ~2-3.5s warm and up to ~5s on a
	// cold connection, so 2.5s timed nearly every call out.
	DefaultTimeout = 5000 * time.Millisecond
	// MaxTokens bounds the answer on models that think (or might: unknown
	// models). A full FFA answer is ~150-300 output tokens; the headroom covers
	// low-effort thinking on models where it cannot be turned off. Hitting it is
	// an error (truncated JSON), never a partial act.
	MaxTokens = 2048
	// MaxTokensNoThinking bounds the answer on models whose request shape turns
	// thinking off: only the JSON answer counts, ~200-300 tokens with the top-3
	// probability lists, so 1024 is >3x headroom.
	MaxTokensNoThinking = 1024
)

// instrumentationName scopes the tracer and meter.
const instrumentationName = "github.com/joustmania/agent/decide/backends/claude"

// Config configures one Backend. It is built by the caller from flags (model,
// timeout, capture) and env (APIKey) on every decision.
type Config struct {
	// APIKey is the Anthropic API key (ANTHROPIC_API_KEY). Empty = unavailable.
	APIKey string
	// BaseURL overrides DefaultBaseURL (tests, or a proxy). The agent sets it from
	// the decision.claude.base_url flag (#1900), key-safe checked in the factory.
	BaseURL string
	// Model is the model id, e.g. "claude-haiku-4-5". Empty = unavailable.
	Model string
	// Timeout bounds one Decide call; not positive means DefaultTimeout.
	Timeout time.Duration
	// HTTPClient overrides the shared default client. Leave nil in production:
	// the default is shared across New calls so the TLS connection to the API
	// is reused: a cold connection measured ~1-2s slower on the first call (#1231).
	HTTPClient *http.Client
	// CaptureContent records the prompt and model output on the span
	// (gen_ai.system_instructions / gen_ai.input.messages /
	// gen_ai.output.messages). Off by default: content is opt-in per semconv.
	CaptureContent bool
}

// Backend is the claude decision backend. It implements decide.Backend.
type Backend struct {
	cfg    Config
	host   string
	port   int
	client anthropic.Client
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
	host, port := anthropichttp.ServerAddress(cfg.BaseURL)
	return &Backend{cfg: cfg, host: host, port: port, client: newClient(cfg)}
}

// Name implements decide.Backend.
func (b *Backend) Name() string { return Name }

// Model is the configured model id (trimmed; empty when unset).
func (b *Backend) Model() string { return b.cfg.Model }

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
	if err := llmio.Supported(req); err != nil {
		return decide.Response{}, fmt.Errorf("%s: %w", Name, err)
	}

	ctx, cancel := context.WithTimeout(ctx, b.cfg.Timeout)
	defer cancel()

	prompt := llmio.Render(req)
	shape := shapeFor(b.cfg.Model)
	params := b.params(prompt, shape)

	ctx, span := otel.Tracer(instrumentationName).Start(ctx,
		"chat "+b.cfg.Model,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(b.requestAttributes(shape, prompt.System)...),
	)
	defer span.End()
	if b.cfg.CaptureContent {
		span.SetAttributes(llmio.InputContentAttributes(prompt)...)
	}

	start := time.Now()
	msg, err := b.client.Messages.New(ctx, params)
	call := callResult{msg: msg, start: start}

	if err != nil {
		call.errType = anthropichttp.ErrorType(ctx, err)
		b.finish(ctx, span, call, nil)
		return decide.Response{}, fmt.Errorf("claude: %s: %w", call.errType, err)
	}

	text, thinking := contentOf(msg)
	call.text, call.thinking = text, thinking

	switch msg.StopReason {
	case anthropic.StopReasonEndTurn, anthropic.StopReasonStopSequence:
	case anthropic.StopReasonRefusal:
		// Server-side refusal fallbacks are deliberately not requested: a second
		// model call does not fit the in-game budget, and rules is the fallback.
		call.errType = "refusal"
		if cat := string(msg.StopDetails.Category); cat != "" {
			span.SetAttributes(attrRefusalCategory.String(cat))
		}
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

// newClient builds the SDK client once per Backend. The pooled connections
// live in the (shared) http.Client. anthropichttp.NewClient takes the key and
// base URL from Config only, turns SDK retries and SDK OTel off (Decide owns
// the single chat span; the outcome that matters, parse/validation and the
// rationale as a reasoning part, is only known after the SDK returns), injects
// traceparent and never follows a redirect.
func newClient(cfg Config) anthropic.Client {
	return anthropichttp.NewClient(cfg.APIKey, cfg.BaseURL, cfg.HTTPClient)
}

// attrRefusalCategory is the stop_details.category of a refusal. Semconv has
// no name for it; the anthropic.* prefix follows the provider-specific
// attribute convention.
const attrRefusalCategory = attribute.Key("anthropic.refusal.category")

// requestShape is the per-model latency tuning: thinking off where the model
// allows it cleanly, low effort elsewhere, temperature 0 where sampling
// parameters are accepted, and max_tokens sized to the answer.
type requestShape struct {
	thinking    anthropic.ThinkingConfigParamUnion
	effort      anthropic.OutputConfigEffort
	temperature *float64
	// maxTokens is the request max_tokens; zero means MaxTokens.
	maxTokens int64
}

// maxTokensOf is the request max_tokens for a shape.
func (s requestShape) maxTokensOf() int64 {
	if s.maxTokens > 0 {
		return s.maxTokens
	}
	return MaxTokens
}

// shapeFor picks the request shape for model by EXACT id (after stripping a
// dated-snapshot suffix such as "-20251001"). Prefix matching is deliberately
// avoided: a future id like "claude-sonnet-5-6" must not inherit a shape (e.g.
// thinking "disabled") that its API might reject with a 400 on every call.
// Unknown models get no extra fields.
//
// Opus 5.5 is listed for completeness; even at low effort it is not expected
// to fit the in-game budget (Fable is intentionally absent).
func shapeFor(model string) requestShape {
	zero := 0.0
	switch anthropichttp.StripDateSuffix(model) {
	case "claude-haiku-4-5":
		// No thinking unless asked for; sampling parameters accepted; effort is not.
		return requestShape{temperature: &zero, maxTokens: MaxTokensNoThinking}
	case "claude-sonnet-5-5":
		// "disabled" is a 400 here. Adaptive thinking at low effort, the
		// recommended way to cut thinking on this model: at low it skips
		// thinking on simple requests (0 thinking tokens in every live call,
		// #1231) at the same latency as between_tools. between_tools (thinking
		// fully off) was used before and returned a schema-valid but empty answer
		// (question id "x" or "", rejected by Validate) on 5 of 21 live calls;
		// adaptive+low on 0 of 14. MaxTokens leaves room should it think.
		return requestShape{
			thinking: anthropic.ThinkingConfigParamUnion{OfAdaptive: &anthropic.ThinkingConfigAdaptiveParam{}},
			effort:   anthropic.OutputConfigEffortLow,
		}
	case "claude-opus-5-5":
		// Thinking cannot be disabled; low effort keeps it short.
		return requestShape{effort: anthropic.OutputConfigEffortLow}
	case "claude-opus-5", "claude-sonnet-5":
		// Thinking on by default; disabled is accepted at effort high or below.
		return requestShape{
			thinking:  anthropic.ThinkingConfigParamUnion{OfDisabled: &anthropic.ThinkingConfigDisabledParam{}},
			effort:    anthropic.OutputConfigEffortLow,
			maxTokens: MaxTokensNoThinking,
		}
	default:
		return requestShape{}
	}
}

func (b *Backend) params(p llmio.Prompt, shape requestShape) anthropic.MessageNewParams {
	params := anthropic.MessageNewParams{
		Model:     anthropic.Model(b.cfg.Model),
		MaxTokens: shape.maxTokensOf(),
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

func (b *Backend) requestAttributes(shape requestShape, system string) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameAnthropic,
		semconv.GenAIRequestModel(b.cfg.Model),
		semconv.GenAIRequestMaxTokens(int(shape.maxTokensOf())),
		semconv.GenAIOutputTypeJSON,
		semconv.ServerAddress(b.host),
		semconv.ServerPort(b.port),
		anthropichttp.AttrSystemSHA256.String(anthropichttp.SystemSHA(system)),
	}
	if shape.temperature != nil {
		attrs = append(attrs, semconv.GenAIRequestTemperature(*shape.temperature))
	}
	if shape.effort != "" {
		attrs = append(attrs, anthropichttp.AttrRequestReasoningLevel.String(string(shape.effort)))
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
	if c.msg == nil {
		// Transport failure: no response, so the generation ended in error.
		span.SetAttributes(semconv.GenAIResponseFinishReasons(llmio.FinishError))
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
		// pause_turn and any future stop reason: not in the semconv enum.
		return llmio.FinishError
	}
}
