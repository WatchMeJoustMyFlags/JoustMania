// Package inference holds the agent's text-generation backend for the OFFLINE
// LLM seams (#1234): the shadow-experiment proposer (experiment.Backend) and the
// post-game retrospective (decision.TextBackend). Both take the same narrow
// shape, Infer(ctx, llm.Prompt) (string, error): send System+User text, get the
// model's raw text back. The callers own the prompt and the defensive decoding,
// so a backend needs no tool calling, streaming or agent loop.
//
// In-game decisions do NOT use this package: they go through the typed decide
// contract (decide/backends/*, selected by the decision.backend flag).
//
// The only real implementation is Claude, called directly through the official
// Go SDK (no gateway, per docs/NORTHSTAR.md). main.go selects it with
// the inference.backend flag (claude); the default is the stub, so nothing needs a key.
//
// GRACEFUL DEGRADATION: every failure (no key, HTTP error, timeout, refusal,
// truncation, empty reply) is a clean error and never a panic. The proposer and
// the retro already treat any error as "no proposal / no conclusion this time".
package inference

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/shared/constant"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/internal/anthropichttp"
	"github.com/joustmania/agent/llm"
)

// Name is the backend name recorded as inference.used on the retro span.
const Name = "claude"

const (
	// DefaultBaseURL is the Anthropic API root.
	DefaultBaseURL = "https://api.anthropic.com"
	// DefaultModel is the default model for the offline proposer/retro calls.
	DefaultModel = "claude-opus-5-5"
	// DefaultEffort is output_config.effort for models that accept it. Set
	// explicitly because Claude Opus 5.5 defaults to medium and other models to
	// high; the offline analysis does not need more.
	DefaultEffort = "medium"
	// DefaultTimeout bounds one Infer call. These calls are off the game's hot path
	// (game end, proposer cadence), so the budget is generous; a caller ctx
	// deadline that is sooner still wins.
	DefaultTimeout = 120 * time.Second
	// MaxTokens bounds one reply. The retro and proposal replies are a single small
	// JSON object; the headroom covers adaptive thinking on models that think.
	MaxTokens = 16000
)

// instrumentationName scopes the tracer.
const instrumentationName = "github.com/joustmania/agent/inference"

// Settings are the per-call tuning values (#1788): the model, the effort and the
// call deadline. Blank or non-positive fields fall back like the Config fields.
type Settings struct {
	Model   string
	Effort  string
	Timeout time.Duration
}

// Config configures a Claude backend. main.go builds it once at startup; the
// tuning (Model, Effort, Timeout) is resolved per call through Tuning when set.
type Config struct {
	// APIKey is the Anthropic API key (ANTHROPIC_API_KEY). Empty = unavailable.
	APIKey string
	// BaseURL overrides DefaultBaseURL (tests).
	BaseURL string
	// Model is the model id; empty means DefaultModel.
	Model string
	// Effort is output_config.effort (low|medium|high|xhigh|max). Empty sends none.
	// It is never sent to Claude Haiku 4.5, which rejects the parameter.
	Effort string
	// Timeout bounds one call; not positive means DefaultTimeout.
	Timeout time.Duration
	// Tuning, when set, is called on EVERY Infer to resolve the model, effort and
	// timeout at use time (#1788, FR-150: the inference.* agent flags, env and
	// compiled defaults as fallbacks). Nil uses Model / Effort / Timeout above.
	Tuning func(ctx context.Context) Settings
	// HTTPClient overrides the default client (tests).
	HTTPClient *http.Client
}

// Claude is the direct Anthropic text backend. Build it with NewClaude.
type Claude struct {
	cfg    Config
	host   string
	port   int
	client anthropic.Client
}

// ErrUnavailable is returned by Infer when no API key is configured.
var ErrUnavailable = errors.New("inference: claude backend unavailable (no API key)")

// ErrEmptyReply is returned when the model produced no text.
var ErrEmptyReply = errors.New("inference: claude returned no text")

// Error is a failed Infer call. Type is the low-cardinality error.type (an HTTP
// status code, "timeout", "canceled", "refusal", "max_tokens", "empty_reply",
// "unavailable", ...) that callers may put on spans; Error() may carry vendor
// text and belongs in logs at most, never on a span (#1242).
type Error struct {
	Type string
	Err  error
}

func (e *Error) Error() string { return "inference: claude: " + e.Type + ": " + e.Err.Error() }

func (e *Error) Unwrap() error { return e.Err }

// ErrorType returns Type. Callers outside this package can classify an error
// through the interface{ ErrorType() string } without importing it.
func (e *Error) ErrorType() string { return e.Type }

// ErrorType returns the low-cardinality type of an Infer error: Error.Type when err
// wraps an *Error, else "timeout" / "canceled" for context errors, else "_OTHER".
func ErrorType(err error) string {
	var ie *Error
	switch {
	case errors.As(err, &ie):
		return ie.Type
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	case errors.Is(err, context.Canceled):
		return "canceled"
	}
	return semconv.ErrorTypeOther.Value.AsString()
}

// NewClaude builds a Claude backend. It never fails; without an API key every
// Infer returns ErrUnavailable.
func NewClaude(cfg Config) *Claude {
	cfg.BaseURL = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	if cfg.BaseURL == "" {
		cfg.BaseURL = DefaultBaseURL
	}
	static := normalizeSettings(Settings{Model: cfg.Model, Effort: cfg.Effort, Timeout: cfg.Timeout})
	cfg.Model, cfg.Effort, cfg.Timeout = static.Model, static.Effort, static.Timeout
	if cfg.HTTPClient == nil {
		// No client-wide timeout: the timeout is resolved per call (Tuning) and every
		// Infer bounds its request with a ctx deadline, which is the real budget.
		// The redirect guard lives in anthropichttp.NewClient, so it covers an
		// injected client too.
		cfg.HTTPClient = &http.Client{}
	}
	host, port := anthropichttp.ServerAddress(cfg.BaseURL)
	return &Claude{cfg: cfg, host: host, port: port, client: newClient(cfg)}
}

// Name is the backend name ("claude").
func (c *Claude) Name() string { return Name }

// BaseURL is the API root the backend sends requests (and the API key) to.
func (c *Claude) BaseURL() string { return c.cfg.BaseURL }

// Model is the model id the next call uses (resolved through Tuning when set).
func (c *Claude) Model() string { return c.settings(context.Background()).Model }

// settings resolves the tuning for one call: Tuning when set, else the static
// Config values; blank or invalid values are normalised.
func (c *Claude) settings(ctx context.Context) Settings {
	if c.cfg.Tuning == nil {
		return Settings{Model: c.cfg.Model, Effort: c.cfg.Effort, Timeout: c.cfg.Timeout}
	}
	return normalizeSettings(c.cfg.Tuning(ctx))
}

// normalizeSettings applies the defaults: a blank model is DefaultModel, an effort
// outside low|medium|high|xhigh|max is dropped (none is sent), a non-positive
// timeout is DefaultTimeout.
func normalizeSettings(s Settings) Settings {
	s.Model = strings.TrimSpace(s.Model)
	if s.Model == "" {
		s.Model = DefaultModel
	}
	s.Effort = strings.ToLower(strings.TrimSpace(s.Effort))
	if s.Effort != "" && !validEfforts[s.Effort] {
		slog.Warn("inference effort is not one of low|medium|high|xhigh|max; sending no effort",
			"value", s.Effort)
		s.Effort = ""
	}
	if s.Timeout <= 0 {
		s.Timeout = DefaultTimeout
	}
	return s
}

// Available reports whether an API key is configured.
func (c *Claude) Available() bool {
	return c != nil && strings.TrimSpace(c.cfg.APIKey) != ""
}

// Infer sends prompt.System / prompt.User and returns the concatenated text of
// the reply. prompt.Model is ignored: the backend's configured model is used.
func (c *Claude) Infer(ctx context.Context, prompt llm.Prompt) (string, error) {
	if !c.Available() {
		return "", &Error{Type: "unavailable", Err: ErrUnavailable}
	}
	s := c.settings(ctx)
	ctx, cancel := context.WithTimeout(ctx, s.Timeout)
	defer cancel()

	ctx, span := otel.Tracer(instrumentationName).Start(ctx, "chat "+s.Model,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(c.requestAttributes(prompt.System, s)...),
	)
	defer span.End()

	msg, err := c.client.Beta.Messages.New(ctx, params(prompt, s))
	if err != nil {
		// Only the low-cardinality error.type reaches the span: vendor error bodies
		// can echo request details, so the error text never does (#1242).
		errType := anthropichttp.ErrorType(ctx, err)
		fail(span, errType)
		return "", &Error{Type: errType, Err: err}
	}
	c.recordResponse(span, msg)

	switch msg.StopReason {
	case anthropic.BetaStopReasonEndTurn, anthropic.BetaStopReasonStopSequence:
	case anthropic.BetaStopReasonRefusal:
		return "", stopError(span, "refusal", msg.StopReason)
	case anthropic.BetaStopReasonMaxTokens:
		return "", stopError(span, "max_tokens", msg.StopReason)
	default:
		return "", stopError(span, "stop_reason_"+string(msg.StopReason), msg.StopReason)
	}

	text := textOf(msg)
	if strings.TrimSpace(text) == "" {
		fail(span, "empty_reply")
		return "", &Error{Type: "empty_reply", Err: ErrEmptyReply}
	}
	return text, nil
}

// newClient builds the SDK client, shared with the decide claude backend:
// credentials and base URL come from Config only (no ANTHROPIC_* env defaults),
// retries are off (the callers degrade on error), the SDK's own OTel is off
// (Infer owns the single chat span), W3C traceparent is injected and no
// redirect is ever followed.
func newClient(cfg Config) anthropic.Client {
	return anthropichttp.NewClient(cfg.APIKey, cfg.BaseURL, cfg.HTTPClient)
}

// fallbackModels accept the server-side refusal fallback in its "default" form on
// the Claude API: a policy decline is re-run on a fallback model in the same call.
var fallbackModels = map[string]bool{
	"claude-fable-5-1":  true,
	"claude-opus-5-5":   true,
	"claude-opus-5":     true,
	"claude-sonnet-5-5": true,
}

// validEfforts are the output_config.effort levels the API accepts.
var validEfforts = map[string]bool{"low": true, "medium": true, "high": true, "xhigh": true, "max": true}

// noEffortModels reject output_config.effort (a 400 if sent).
var noEffortModels = map[string]bool{
	"claude-haiku-4-5":  true,
	"claude-sonnet-4-5": true,
}

func usesFallback(s Settings) bool { return fallbackModels[anthropichttp.StripDateSuffix(s.Model)] }

func effort(s Settings) string {
	if noEffortModels[anthropichttp.StripDateSuffix(s.Model)] {
		return ""
	}
	return s.Effort
}

func params(p llm.Prompt, s Settings) anthropic.BetaMessageNewParams {
	params := anthropic.BetaMessageNewParams{
		Model:     anthropic.Model(s.Model),
		MaxTokens: MaxTokens,
		Messages: []anthropic.BetaMessageParam{
			anthropic.NewBetaUserMessage(anthropic.NewBetaTextBlock(p.User)),
		},
	}
	if p.System != "" {
		params.System = []anthropic.BetaTextBlockParam{{Text: p.System}}
	}
	if e := effort(s); e != "" {
		params.OutputConfig = anthropic.BetaOutputConfigParam{Effort: anthropic.BetaOutputConfigEffort(e)}
	}
	if usesFallback(s) {
		params.Betas = []anthropic.AnthropicBeta{anthropic.AnthropicBetaServerSideFallback2026_07_01}
		params.Fallbacks = anthropic.BetaFallbacksParamUnion{OfDefault: constant.ValueOf[constant.Default]()}
	}
	return params
}

func (c *Claude) requestAttributes(system string, s Settings) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameAnthropic,
		semconv.GenAIRequestModel(s.Model),
		semconv.GenAIRequestMaxTokens(MaxTokens),
		semconv.ServerAddress(c.host),
		semconv.ServerPort(c.port),
		anthropichttp.AttrSystemSHA256.String(anthropichttp.SystemSHA(system)),
	}
	if e := effort(s); e != "" {
		attrs = append(attrs, anthropichttp.AttrRequestReasoningLevel.String(e))
	}
	return attrs
}

func (c *Claude) recordResponse(span trace.Span, m *anthropic.BetaMessage) {
	if m == nil {
		return
	}
	input := int(m.Usage.InputTokens + m.Usage.CacheReadInputTokens + m.Usage.CacheCreationInputTokens)
	span.SetAttributes(
		semconv.GenAIResponseID(m.ID),
		semconv.GenAIResponseModel(string(m.Model)),
		semconv.GenAIResponseFinishReasons(string(m.StopReason)),
		// Per the semconv Anthropic page, input_tokens includes cached tokens.
		semconv.GenAIUsageInputTokens(input),
		semconv.GenAIUsageOutputTokens(int(m.Usage.OutputTokens)),
		semconv.GenAIUsageCacheReadInputTokens(int(m.Usage.CacheReadInputTokens)),
		semconv.GenAIUsageCacheCreationInputTokens(int(m.Usage.CacheCreationInputTokens)),
	)
}

func stopError(span trace.Span, errType string, reason anthropic.BetaStopReason) error {
	fail(span, errType)
	return &Error{Type: errType, Err: fmt.Errorf("stop_reason %s", reason)}
}

func fail(span trace.Span, errType string) {
	span.SetAttributes(semconv.ErrorTypeKey.String(errType))
	span.SetStatus(codes.Error, errType)
}

// textOf concatenates the reply's text blocks (thinking and fallback blocks are
// skipped).
func textOf(m *anthropic.BetaMessage) string {
	var b strings.Builder
	for _, block := range m.Content {
		if t, ok := block.AsAny().(anthropic.BetaTextBlock); ok {
			b.WriteString(t.Text)
		}
	}
	return b.String()
}
