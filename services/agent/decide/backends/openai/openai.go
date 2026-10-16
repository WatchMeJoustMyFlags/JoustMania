// Package openai is the "openai" decision backend of epic #1225 (#1228): a
// direct call to any hosted OpenAI-compatible /chat/completions endpoint (OpenAI,
// Gemini's OpenAI endpoint, Mistral, Groq, DeepSeek, ...), no gateway, that
// answers the typed decide questions with structured output.
//
// It is pure: it reads no flags and does not import package decision. The
// caller (decision wiring, #1226) builds a Config from flags
// (decision.openai.base_url / .model / .timeout_ms, decision.capture_content)
// and env (DECISION_OPENAI_API_KEY) on every call, so the vendor and model
// switch live, and runs decide.Validate on the Response (Decide also
// validates, via llmio.Parse).
//
// Transport: plain net/http (no SDK, CGO-free, no new deps), the same shape as
// inference/openai.go: Bearer auth, W3C traceparent injected from the chat
// span, a bounded body read, no retries (inside a ~2.5s in-game budget the
// rules backend is the retry).
//
// Structured output: response_format json_schema (strict, llmio.AnswerSchema).
// Some OpenAI-compatible vendors reject json_schema with a 400; Decide then
// retries ONCE with response_format json_object (valid JSON, the shape comes
// from the prompt; llmio.Parse and decide.Validate enforce it) and, if that
// request is accepted, remembers the downgrade for this base URL + model (see
// schemaRejected) so later calls skip the doomed first attempt.
//
// Telemetry mirrors the claude backend: one CLIENT span per HTTP call named
// "chat {model}" with the OTel GenAI semantic conventions (v1.41.0),
// gen_ai.provider.name derived from the base URL host (ProviderName), opt-in
// content capture with reasoning text as "reasoning" output parts, and the
// GenAI client metrics.
package openai

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/semconv/v1.41.0/genaiconv"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/backends/internal/chathttp"
	"github.com/joustmania/agent/decide/llmio"
)

// Name is the backend name (the decision.backend flag value).
const Name = "openai"

const (
	// DefaultBaseURL is the OpenAI API root, the decision.openai.base_url flag
	// default. New does NOT fill it in: an empty BaseURL makes the backend
	// unavailable, so the caller decides the default (the flag).
	DefaultBaseURL = "https://api.openai.com/v1"
	// DefaultTimeout is the per-call budget when Config.Timeout is not positive.
	DefaultTimeout = 2500 * time.Millisecond
	// MaxTokens bounds the answer. A full FFA answer is ~150-300 output
	// tokens; the headroom covers low-effort reasoning on reasoning models.
	// Hitting it is an error (truncated JSON), never a partial act.
	MaxTokens = 2048
)

// instrumentationName scopes the tracer and meter.
const instrumentationName = "github.com/joustmania/agent/decide/backends/openai"

var metrics = chathttp.NewMetrics(instrumentationName)

// attrRequestReasoningLevel is gen_ai.request.reasoning.level, which postdates
// the vendored semconv v1.41.0 (same key as the claude backend).
const attrRequestReasoningLevel = attribute.Key("gen_ai.request.reasoning.level")

// attrResponseFormat records which response_format was sent (json_schema or the
// json_object fallback). semconv has no attribute for it.
const attrResponseFormat = attribute.Key("decide.response_format")

// Config configures one Backend. It is built by the caller from flags and env
// on every decision.
type Config struct {
	// APIKey is the vendor API key (DECISION_OPENAI_API_KEY), sent as
	// "Authorization: Bearer". Empty = unavailable.
	APIKey string
	// BaseURL is the OpenAI-compatible API root; "/chat/completions" is
	// appended (e.g. "https://api.openai.com/v1",
	// "https://generativelanguage.googleapis.com/v1beta/openai"). Empty =
	// unavailable.
	BaseURL string
	// Model is the model id. Empty = unavailable.
	Model string
	// Timeout bounds one Decide call (both attempts when the json_object
	// fallback kicks in); not positive means DefaultTimeout.
	Timeout time.Duration
	// HTTPClient overrides the shared default client. Leave nil in production
	// so connections are pooled across the per-decision New calls.
	HTTPClient *http.Client
	// CaptureContent records the prompt and model output on the span
	// (gen_ai.system_instructions / gen_ai.input.messages /
	// gen_ai.output.messages). Off by default: content is opt-in per semconv.
	CaptureContent bool
}

// Backend is the openai decision backend. It implements decide.Backend.
type Backend struct {
	cfg      Config
	host     string
	port     int
	provider string
	// schemaRejected is set once the endpoint rejected json_schema with a 400
	// and accepted json_object; later calls go straight to json_object.
	schemaRejected atomic.Bool
}

// schemaRejectedEndpoints carries the json_schema downgrade across Backend
// instances (the caller builds a new Backend per decision), keyed by
// base URL + model. Without it every decision against such a vendor would pay
// a doomed json_schema round-trip first.
var schemaRejectedEndpoints sync.Map // endpointKey -> struct{}

func endpointKey(baseURL, model string) string { return baseURL + "\x00" + model }

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
	b := &Backend{cfg: cfg, host: host, port: port, provider: ProviderName(cfg.BaseURL)}
	if _, ok := schemaRejectedEndpoints.Load(endpointKey(cfg.BaseURL, cfg.Model)); ok {
		b.schemaRejected.Store(true)
	}
	return b
}

// Name implements decide.Backend.
func (b *Backend) Name() string { return Name }

// Provider is the gen_ai.provider.name this backend records.
func (b *Backend) Provider() string { return b.provider }

// Available reports whether the backend is configured: an API key, a model and
// a base URL.
func (b *Backend) Available() bool {
	return b != nil && strings.TrimSpace(b.cfg.APIKey) != "" && b.cfg.Model != "" && b.cfg.BaseURL != ""
}

// ErrUnavailable is returned by Decide when Available is false.
var ErrUnavailable = errors.New("openai: backend unavailable (no API key, model or base URL)")

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
	if b.schemaRejected.Load() {
		resp, _, err := b.attempt(ctx, req, prompt, formatJSONObject)
		return resp, err
	}
	resp, status, err := b.attempt(ctx, req, prompt, formatJSONSchema)
	if status != http.StatusBadRequest {
		return resp, err
	}
	// A 400 on json_schema: retry once with json_object. Only an HTTP answer
	// other than 400 proves json_object is accepted (so the 400 was about the
	// schema, not, say, an unknown model); only then is the downgrade
	// remembered.
	resp, status, err = b.attempt(ctx, req, prompt, formatJSONObject)
	if status != 0 && status != http.StatusBadRequest {
		b.schemaRejected.Store(true)
		schemaRejectedEndpoints.Store(endpointKey(b.cfg.BaseURL, b.cfg.Model), struct{}{})
	}
	return resp, err
}

// attempt makes one /chat/completions call under its own chat span. status is
// the HTTP status, 0 when there was no HTTP response.
func (b *Backend) attempt(ctx context.Context, req decide.Request, prompt llmio.Prompt, format string) (decide.Response, int, error) {
	shape := shapeFor(b.provider, b.cfg.Model)
	body, err := json.Marshal(b.request(prompt, shape, format))
	if err != nil {
		return decide.Response{}, 0, fmt.Errorf("openai: marshal request: %w", err)
	}

	ctx, span := otel.Tracer(instrumentationName).Start(ctx,
		"chat "+b.cfg.Model,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(b.requestAttributes(shape, format)...),
	)
	defer span.End()
	if b.cfg.CaptureContent {
		span.SetAttributes(llmio.InputContentAttributes(prompt)...)
	}

	start := time.Now()
	header := http.Header{"Authorization": []string{"Bearer " + b.cfg.APIKey}}
	status, raw, err := chathttp.Post(ctx, b.cfg.HTTPClient, b.cfg.BaseURL+"/chat/completions", header, body, chathttp.MaxResponseBytes)
	call := callResult{start: start}

	switch {
	case errors.Is(err, chathttp.ErrTooLarge):
		call.errType = "response_too_large"
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: %w: response body over %d bytes", llmio.ErrMalformed, chathttp.MaxResponseBytes)
	case err != nil:
		call.errType = chathttp.TransportErrorType(ctx, err)
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: %s: %w", call.errType, err)
	case status < 200 || status > 299:
		call.errType = chathttp.StatusErrorType(status)
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: status %d: %s", status, clip(string(raw), 256))
	}

	var cr chatResponse
	if err := json.Unmarshal(raw, &cr); err != nil {
		call.errType = "invalid_response"
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: %w: unparseable response body: %v", llmio.ErrMalformed, err)
	}
	call.resp = &cr
	if cr.Error != nil && cr.Error.Message != "" {
		call.errType = semconv.ErrorTypeOther.Value.AsString()
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: endpoint error: %s", clip(cr.Error.Message, 256))
	}
	if len(cr.Choices) == 0 {
		call.errType = "no_choices"
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: %w: no choices", llmio.ErrMalformed)
	}
	choice := cr.Choices[0]
	call.text, call.reasoning = choice.Message.Content.text, choice.reasoning()

	switch {
	case choice.Message.Refusal != "":
		call.errType = "refusal"
	case choice.FinishReason == "stop" || choice.FinishReason == "":
	case choice.FinishReason == "length":
		call.errType = "max_tokens"
	default:
		call.errType = "finish_reason_" + choice.FinishReason
	}
	if call.errType != "" {
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("%w: openai: %s", decide.ErrInvalid, call.errType)
	}

	resp, perr := llmio.Parse(req, []byte(call.text), Name)
	if perr != nil {
		call.errType = "invalid_output"
		b.finish(ctx, span, call, nil)
		return decide.Response{}, status, fmt.Errorf("openai: %w", perr)
	}
	b.finish(ctx, span, call, &resp)
	return resp, status, nil
}

// requestShape is the per-vendor/model parameter set: OpenAI's own API takes
// max_completion_tokens (its reasoning models reject max_tokens) and its
// reasoning models reject a temperature other than 1 but take
// reasoning_effort; every other vendor gets the classic max_tokens +
// temperature 0.
type requestShape struct {
	maxCompletionTokens bool
	temperature         *float64
	reasoningEffort     string
}

// openAIReasoningModel matches OpenAI reasoning model ids (o1, o3-mini, o4-mini,
// gpt-5, gpt-5.5-mini, ...).
var openAIReasoningModel = regexp.MustCompile(`^(o\d|gpt-5)`)

func shapeFor(provider, model string) requestShape {
	zero := 0.0
	if provider != string(genaiconv.ProviderNameOpenAI) {
		return requestShape{temperature: &zero}
	}
	if openAIReasoningModel.MatchString(model) {
		return requestShape{maxCompletionTokens: true, reasoningEffort: "low"}
	}
	return requestShape{maxCompletionTokens: true, temperature: &zero}
}

func (b *Backend) request(p llmio.Prompt, shape requestShape, format string) chatRequest {
	r := chatRequest{
		Model: b.cfg.Model,
		Messages: []chatMessage{
			{Role: "system", Content: p.System},
			{Role: "user", Content: p.User},
		},
		ResponseFormat:  responseFormat{Type: format},
		Temperature:     shape.temperature,
		ReasoningEffort: shape.reasoningEffort,
	}
	if format == formatJSONSchema {
		r.ResponseFormat.JSONSchema = &jsonSchemaFormat{Name: schemaName, Strict: true, Schema: llmio.AnswerSchema()}
	}
	if shape.maxCompletionTokens {
		r.MaxCompletionTokens = MaxTokens
	} else {
		r.MaxTokens = MaxTokens
	}
	return r
}

func (b *Backend) requestAttributes(shape requestShape, format string) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameKey.String(b.provider),
		semconv.GenAIRequestModel(b.cfg.Model),
		semconv.GenAIRequestMaxTokens(MaxTokens),
		semconv.GenAIOutputTypeJSON,
		semconv.ServerAddress(b.host),
		semconv.ServerPort(b.port),
		attrResponseFormat.String(format),
	}
	if b.provider == string(genaiconv.ProviderNameOpenAI) {
		attrs = append(attrs, semconv.OpenAIAPITypeChatCompletions)
	}
	if shape.temperature != nil {
		attrs = append(attrs, semconv.GenAIRequestTemperature(*shape.temperature))
	}
	if shape.reasoningEffort != "" {
		attrs = append(attrs, attrRequestReasoningLevel.String(shape.reasoningEffort))
	}
	return attrs
}

// callResult is what finish records on the span and metrics.
type callResult struct {
	resp      *chatResponse
	start     time.Time
	text      string
	reasoning []string
	errType   string
}

// finish records the response attributes, content (when captured), error and
// GenAI client metrics.
func (b *Backend) finish(ctx context.Context, span trace.Span, c callResult, resp *decide.Response) {
	duration := time.Since(c.start)
	mc := chathttp.Call{Provider: b.provider, RequestModel: b.cfg.Model, Host: b.host, Port: b.port}
	if r := c.resp; r != nil {
		mc.ResponseModel = r.Model
		if r.ID != "" {
			span.SetAttributes(semconv.GenAIResponseID(r.ID))
		}
		if r.Model != "" {
			span.SetAttributes(semconv.GenAIResponseModel(r.Model))
		}
		reasons := make([]string, 0, len(r.Choices))
		for _, ch := range r.Choices {
			reasons = append(reasons, ch.FinishReason)
		}
		if len(reasons) > 0 {
			span.SetAttributes(semconv.GenAIResponseFinishReasons(reasons...))
		}
		if u := r.Usage; u != nil {
			// OpenAI's prompt_tokens already includes cached tokens, which is
			// what semconv's gen_ai.usage.input_tokens means.
			span.SetAttributes(
				semconv.GenAIUsageInputTokens(u.PromptTokens),
				semconv.GenAIUsageOutputTokens(u.CompletionTokens),
			)
			if d := u.PromptTokensDetails; d != nil {
				span.SetAttributes(semconv.GenAIUsageCacheReadInputTokens(d.CachedTokens))
			}
			if d := u.CompletionTokensDetails; d != nil && d.ReasoningTokens > 0 {
				span.SetAttributes(semconv.GenAIUsageReasoningOutputTokens(d.ReasoningTokens))
			}
			metrics.RecordTokens(ctx, mc, u.PromptTokens, u.CompletionTokens)
		}
		if b.cfg.CaptureContent && len(r.Choices) > 0 {
			rationale := ""
			if resp != nil {
				rationale = resp.Rationale
			}
			span.SetAttributes(llmio.OutputContentAttribute(
				llmio.OutputMessage(c.text, c.reasoning, rationale, finishReason(r.Choices[0].FinishReason))))
		}
	}
	if c.errType != "" {
		span.SetAttributes(semconv.ErrorTypeKey.String(c.errType))
		span.SetStatus(codes.Error, c.errType)
	}
	metrics.RecordDuration(ctx, mc, c.errType, duration)
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
