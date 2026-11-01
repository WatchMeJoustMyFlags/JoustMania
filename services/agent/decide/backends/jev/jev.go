// Package jev is the "jev" decision backend of epic #1225 (#1229): TypeSafe's
// hosted Jev model (System One API, https://docs.typesafe.ai/api), called
// directly (no gateway, no SDK: TypeSafe ships Python/JS SDKs only).
//
// Available() is false until Config.APIKey (JEV_API_KEY) is set, so with
// decision.backend=jev and no key the decision wiring falls back to rules with
// fallback_reason=backend_unavailable. The base URL and model default to
// DefaultBaseURL / DefaultModel. The wire format (typed state + questions, typed
// answers with probabilities and confidence) lives in wire.go and criteria.go.
//
// It is pure: it reads no flags and does not import package decision. The
// caller (decision wiring, #1226) builds a Config from flags
// (decision.jev.base_url / .model / .timeout_ms, decision.capture_content) and
// env (JEV_API_KEY) on every call.
//
// Telemetry mirrors the claude and openai backends: one CLIENT span per HTTP
// call (two for an acting FFA round, tagged jev.stage) named "chat {model}" (just "chat" without a model) with the OTel GenAI
// semantic conventions (v1.41.0) and gen_ai.provider.name=jev (a custom value;
// semconv has no well-known one), opt-in content capture, and the GenAI client
// metrics.
package jev

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
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

// DefaultBaseURL is the Jev API root when Config.BaseURL is empty.
const DefaultBaseURL = "https://api.typesafe.ai/v1"

// DefaultModel is the model when Config.Model is empty: TypeSafe's alias for
// the latest stable Jev (the API requires a model).
const DefaultModel = "jev-latest"

// instrumentationName scopes the tracer and meter.
const instrumentationName = "github.com/joustmania/agent/decide/backends/jev"

var metrics = chathttp.NewMetrics(instrumentationName)

// Config configures one Backend. Same shape as the claude and openai backends.
type Config struct {
	// APIKey is the Jev API key (JEV_API_KEY). Empty = unavailable.
	APIKey string
	// BaseURL is the Jev API root (decision.jev.base_url). Empty means
	// DefaultBaseURL.
	BaseURL string
	// Model is the Jev model id or alias (decision.jev.model), e.g.
	// "jev-latest" or a pinned "jev-1.13.0". Empty means DefaultModel.
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

// New builds a Backend. It never fails; a Config without an API key makes
// Available false and Decide return ErrUnavailable.
func New(cfg Config) *Backend {
	cfg.BaseURL = strings.TrimRight(strings.TrimSpace(cfg.BaseURL), "/")
	if cfg.BaseURL == "" {
		cfg.BaseURL = DefaultBaseURL
	}
	cfg.Model = strings.TrimSpace(cfg.Model)
	if cfg.Model == "" {
		cfg.Model = DefaultModel
	}
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

// Model is the configured model id (trimmed; DefaultModel when unset).
func (b *Backend) Model() string { return b.cfg.Model }

// Available reports whether the backend is configured: an API key. Without it
// the caller falls back to rules (fallback_reason unavailable).
func (b *Backend) Available() bool {
	return b != nil && strings.TrimSpace(b.cfg.APIKey) != ""
}

// ErrUnavailable is returned by Decide when Available is false.
var ErrUnavailable = errors.New("jev: backend unavailable (no JEV_API_KEY)")

// Decide asks Jev to answer req.Questions about req.State. It reads only
// req.State and req.Questions (wire.go's encodeRequest). Every failure is a
// clean error; it never panics. Invalid answers wrap decide.ErrInvalid.
//
// An FFA request is a two-stage round (stages.go, #1262): one call for the
// intervention and, only when the intervention needs them, a second call for
// target and magnitude conditioned on it. Both calls share one Config.Timeout
// deadline and each gets its own chat span (jev.stage). A failed second call
// fails the round (the caller falls back to rules); no half answer is returned.
func (b *Backend) Decide(ctx context.Context, req decide.Request) (decide.Response, error) {
	if !b.Available() {
		return decide.Response{}, ErrUnavailable
	}
	if len(req.Questions) == 0 {
		return decide.Response{}, fmt.Errorf("%w: no questions", decide.ErrInvalid)
	}

	ctx, cancel := context.WithTimeout(ctx, b.cfg.Timeout)
	defer cancel()

	plan := splitStages(req)
	firstResp, err := b.call(ctx, plan.stage, plan.first, "")
	if err != nil {
		return decide.Response{}, err
	}
	if plan.stage == StageSingle {
		return firstResp, nil
	}
	first, stage1, err := plan.resolveGate(firstResp.Answers)
	if err != nil {
		return decide.Response{}, err
	}
	var iv decide.Answer
	for _, a := range first {
		if a.QuestionID == decide.QuestionFFAIntervention {
			iv = a
		}
	}
	details := planDetails(req, iv, plan.details)
	var detailAnswers []decide.Answer
	if len(details.ask.Questions) > 0 {
		detailResp, err := b.call(ctx, StageDetails, details.ask, details.intervention)
		if err != nil {
			return decide.Response{}, fmt.Errorf("jev: details stage after %q: %w", details.intervention, err)
		}
		detailAnswers = detailResp.Answers
	}
	answered, err := details.resolveTwoWay(req.State, append(detailAnswers, details.local...))
	if err != nil {
		return decide.Response{}, err
	}
	resp, err := mergeAnswers(req, first, answered)
	if err != nil {
		return decide.Response{}, err
	}
	resp.Rationale = rationale(resp, stage1)
	return resp, nil
}

// call is one HTTP round trip to Jev for sub (one chat span, tagged with
// stage). chosen is the intervention the details stage is conditioned on ("" for
// the other stages).
func (b *Backend) call(ctx context.Context, stage string, sub decide.Request, chosen string) (decide.Response, error) {
	out, err := encodeRequest(b.cfg.Model, sub, chosen)
	if err != nil {
		return decide.Response{}, err
	}

	spanName := "chat " + b.cfg.Model
	ctx, span := otel.Tracer(instrumentationName).Start(ctx, spanName,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(append(b.requestAttributes(), attribute.String(AttrStage, stage))...),
	)
	defer span.End()
	if b.cfg.CaptureContent {
		span.SetAttributes(semconv.GenAIInputMessagesKey.String(llmio.InputMessagesJSON(out.Content)))
	}

	start := time.Now()
	header := http.Header{}
	setAuth(header, b.cfg.APIKey)
	r, err := chathttp.PostResponse(ctx, b.cfg.HTTPClient, b.cfg.BaseURL+wirePath, header, out.Body, chathttp.MaxResponseBytes)
	if id := r.Header.Get(requestIDHeader); id != "" {
		span.SetAttributes(semconv.GenAIResponseID(id))
	}

	switch {
	case errors.Is(err, chathttp.ErrTooLarge):
		b.finish(ctx, span, start, nil, "response_too_large")
		return decide.Response{}, fmt.Errorf("jev: %w: response body over %d bytes", llmio.ErrMalformed, chathttp.MaxResponseBytes)
	case err != nil:
		errType := chathttp.TransportErrorType(ctx, err)
		b.finish(ctx, span, start, nil, errType)
		return decide.Response{}, fmt.Errorf("jev: %s: %w", errType, err)
	case r.Status < 200 || r.Status > 299:
		b.finish(ctx, span, start, nil, statusErrorType(r.Status, r.Body))
		return decide.Response{}, fmt.Errorf("jev: status %d: %s", r.Status, scrub(clip(string(r.Body), 256)))
	}

	res, errType, err := decodeResponse(sub, chosen, r.Body)
	b.finish(ctx, span, start, &res, errType)
	if err != nil {
		return decide.Response{}, err
	}
	span.SetAttributes(gateAttributes(res.Response)...)
	return res.Response, nil
}

func (b *Backend) requestAttributes() []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIProviderNameKey.String(ProviderName),
		semconv.GenAIOutputTypeJSON,
		semconv.GenAIRequestModel(b.cfg.Model),
		semconv.ServerAddress(b.host),
		semconv.ServerPort(b.port),
	}
}

// AttrNativeConfidence is the chat-span attribute carrying Jev's own
// confidence per question id as a JSON object (e.g.
// {"ffa.intervention":0.31,"ffa.magnitude":0.62}). The decide answers carry the
// confidence decide uses, which differs for ffa.intervention (see wire.go).
const AttrNativeConfidence = "jev.native_confidence"

// Gate span attributes (#1281), on the stage-1 chat span of a gated round:
// Jev's REAL answers. The ffa.intervention answer Decide returns (and the
// agent.decide span's decide.answers) is synthesised from them (stages.go).
const (
	// AttrGatePYes is the gate noul's P(yes): "does the game need an
	// intervention right now?".
	AttrGatePYes = "jev.gate.p_yes"
	// AttrStage1Choice is Jev's pick among the interventions (noop excluded).
	AttrStage1Choice = "jev.stage1.choice"
	// AttrStage1Distribution is Jev's distribution over the interventions
	// (noop excluded) as a JSON object with sorted keys.
	AttrStage1Distribution = "jev.stage1.distribution"
)

// gateAttributes returns the gate span attributes when resp answers the gate
// noul (a gated stage 1), else nil.
func gateAttributes(resp decide.Response) []attribute.KeyValue {
	gate, ok := resp.Find(GateQuestionID)
	if !ok || gate.Noul == nil {
		return nil
	}
	out := []attribute.KeyValue{attribute.Float64(AttrGatePYes, *gate.Noul)}
	if iv, ok := resp.Find(decide.QuestionFFAIntervention); ok {
		out = append(out,
			attribute.String(AttrStage1Choice, iv.Choice),
			attribute.String(AttrStage1Distribution, floatMapJSON(iv.Probabilities)))
	}
	return out
}

// finish records the response attributes, content (when captured), error and
// GenAI client metrics. res is nil when no 2xx body was decoded.
func (b *Backend) finish(ctx context.Context, span trace.Span, start time.Time, res *wireResult, errType string) {
	duration := time.Since(start)
	mc := chathttp.Call{Provider: ProviderName, RequestModel: b.cfg.Model, Host: b.host, Port: b.port}
	if res != nil {
		mc.ResponseModel = res.Model
		if res.Model != "" {
			span.SetAttributes(semconv.GenAIResponseModel(res.Model))
		}
		if nc := floatMapJSON(res.NativeConfidence); nc != "" {
			span.SetAttributes(attribute.String(AttrNativeConfidence, nc))
		}
		if u := res.Usage; u != nil {
			span.SetAttributes(
				semconv.GenAIUsageInputTokens(u.InputTokens),
				semconv.GenAIUsageOutputTokens(u.OutputTokens),
			)
			metrics.RecordTokens(ctx, mc, u.InputTokens, u.OutputTokens)
		}
		if b.cfg.CaptureContent && res.Text != "" {
			finish := llmio.FinishStop
			if errType != "" {
				finish = llmio.FinishError
			}
			span.SetAttributes(llmio.OutputContentAttribute(llmio.OutputMessage(res.Text, nil, "", finish)))
		}
	}
	if errType != "" {
		span.SetAttributes(semconv.ErrorTypeKey.String(errType))
		span.SetStatus(codes.Error, errType)
	}
	metrics.RecordDuration(ctx, mc, errType, duration)
}

// secretPattern matches credential-looking substrings ("sk-...", TypeSafe's
// "apikey_...", "Bearer ...").
var secretPattern = regexp.MustCompile(`(?i)(sk-[a-z0-9_\-]{6,}|apikey_[a-z0-9_\-]{6,}|bearer\s+[a-z0-9._\-]+)`)

// scrub redacts credential-looking substrings from vendor error text. The
// result is still untrusted: callers must not record error strings verbatim on
// spans (use error.type).
func scrub(s string) string { return secretPattern.ReplaceAllString(s, "[redacted]") }

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	for n > 0 && s[n]&0xC0 == 0x80 {
		n--
	}
	return s[:n] + "...(truncated)"
}
