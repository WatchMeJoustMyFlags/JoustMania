package jev

// wire.go is the ONLY file that knows Jev's wire format. Everything else in
// this package (jev.go: availability, timeout, HTTP call, span, metrics) is
// format-agnostic and talks to this file through four things:
//
//	wirePath        the path appended to Config.BaseURL
//	setAuth         the auth header(s)
//	encodeRequest   decide.Request -> request body (+ what to record as content)
//	decodeResponse  2xx response body -> decide.Response (+ telemetry fields)
//
// !!! ASSUMPTION, NOT KNOWLEDGE !!!
// We do not have Jev's API docs yet (#1229). Until we do, this file ASSUMES an
// OpenAI-compatible /chat/completions API: Bearer auth, system+user messages,
// response_format json_schema, the answer as JSON text in
// choices[0].message.content, OpenAI-style usage. That is a placeholder so the
// backend, its telemetry and the contract test exist end to end; it is very
// likely wrong in detail. Jev may well take the typed questions natively and
// return per-option probabilities itself (it is a "System One" style API), in
// which case encodeRequest/decodeResponse stop using llmio's prompt and parser.
//
// To switch to the real format: replace the TODO(jev-api) parts below and the
// hand-written fixtures in testdata/ with recorded Jev traffic, then update the
// expectations in contract_test.go. Nothing outside wire.go + testdata/ +
// contract_test.go should need to change.

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
)

// TODO(jev-api): the real endpoint path (relative to decision.jev.base_url).
const wirePath = "/chat/completions"

// TODO(jev-api): the real auth scheme (Bearer? an X-Api-Key header? signed
// requests?).
func setAuth(h http.Header, apiKey string) {
	h.Set("Authorization", "Bearer "+apiKey)
}

// wireMaxTokens bounds the answer length, if Jev takes such a parameter.
// TODO(jev-api): drop if Jev's API has no output-length knob.
const wireMaxTokens = 2048

// wireRequestOut is what encodeRequest hands to jev.go.
type wireRequestOut struct {
	// Body is the JSON request body.
	Body []byte
	// Content is what gen_ai.system_instructions / gen_ai.input.messages
	// record when content capture is on. For a prompt-based API it is the
	// prompt; for a typed API put the JSON body in Content.User.
	Content llmio.Prompt
	// MaxTokens and Temperature are recorded as gen_ai.request.max_tokens /
	// gen_ai.request.temperature when > 0 / non-nil.
	MaxTokens   int
	Temperature *float64
}

// wireResult is what decodeResponse hands back to jev.go.
type wireResult struct {
	// Response is the parsed and validated answer.
	Response decide.Response
	// ID / Model are gen_ai.response.id / gen_ai.response.model (may be "").
	ID, Model string
	// FinishReasons are the raw gen_ai.response.finish_reasons.
	FinishReasons []string
	// OutputFinishReason is the semconv output-message finish_reason
	// (llmio.Finish*).
	OutputFinishReason string
	// Usage is nil when Jev reports no token usage.
	Usage *wireUsage
	// Text is the raw model output and Reasoning any reasoning text, for the
	// gen_ai.output.messages content capture.
	Text      string
	Reasoning []string
}

type wireUsage struct {
	InputTokens, OutputTokens, ReasoningTokens int
}

// --- TODO(jev-api): request/response structs (ASSUMED OpenAI-compatible) ---

type jevMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type jevJSONSchema struct {
	Name   string         `json:"name"`
	Strict bool           `json:"strict"`
	Schema map[string]any `json:"schema"`
}

type jevResponseFormat struct {
	Type       string        `json:"type"`
	JSONSchema jevJSONSchema `json:"json_schema"`
}

type jevRequest struct {
	Model          string            `json:"model,omitempty"`
	Messages       []jevMessage      `json:"messages"`
	ResponseFormat jevResponseFormat `json:"response_format"`
	MaxTokens      int               `json:"max_tokens"`
	Temperature    float64           `json:"temperature"`
	Stream         bool              `json:"stream"`
}

type jevResponse struct {
	ID      string `json:"id"`
	Model   string `json:"model"`
	Choices []struct {
		Message struct {
			Content          *string `json:"content"`
			ReasoningContent string  `json:"reasoning_content"`
			Refusal          string  `json:"refusal"`
		} `json:"message"`
		FinishReason string `json:"finish_reason"`
	} `json:"choices"`
	Usage *struct {
		PromptTokens            int `json:"prompt_tokens"`
		CompletionTokens        int `json:"completion_tokens"`
		CompletionTokensDetails *struct {
			ReasoningTokens int `json:"reasoning_tokens"`
		} `json:"completion_tokens_details"`
	} `json:"usage"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// --- mapping ---

// encodeRequest maps req (State + Questions only, never Context) to Jev's
// request body.
// TODO(jev-api): ASSUMED OpenAI-compatible: the llmio prompt as system+user
// messages and llmio.AnswerSchema as a strict json_schema response format.
func encodeRequest(model string, req decide.Request) (wireRequestOut, error) {
	prompt := llmio.Render(req)
	zero := 0.0
	body, err := json.Marshal(jevRequest{
		Model: model,
		Messages: []jevMessage{
			{Role: "system", Content: prompt.System},
			{Role: "user", Content: prompt.User},
		},
		ResponseFormat: jevResponseFormat{
			Type:       "json_schema",
			JSONSchema: jevJSONSchema{Name: "joustmania_decision", Strict: true, Schema: llmio.AnswerSchema()},
		},
		MaxTokens:   wireMaxTokens,
		Temperature: zero,
	})
	if err != nil {
		return wireRequestOut{}, fmt.Errorf("jev: marshal request: %w", err)
	}
	return wireRequestOut{Body: body, Content: prompt, MaxTokens: wireMaxTokens, Temperature: &zero}, nil
}

// errEndpoint is returned for an error envelope in a 2xx body (not an
// invalid answer, so it does not wrap decide.ErrInvalid).
var errEndpoint = errors.New("jev: endpoint error")

// decodeResponse maps a 2xx response body to a validated decide.Response.
// On failure it returns a non-empty low-cardinality errType (the span's
// error.type) and an error; malformed or invalid answers wrap
// decide.ErrInvalid. The partially filled wireResult is still returned so the
// span gets whatever telemetry the body carried.
// TODO(jev-api): ASSUMED OpenAI-compatible response; the answer is JSON text
// parsed with llmio.Parse. If Jev returns typed answers with probabilities,
// map them to decide.Answer here and call decide.Validate directly.
func decodeResponse(req decide.Request, body []byte) (wireResult, string, error) {
	var r jevResponse
	if err := json.Unmarshal(body, &r); err != nil {
		return wireResult{}, "invalid_response", fmt.Errorf("jev: %w: unparseable response body: %v", llmio.ErrMalformed, err)
	}
	res := wireResult{ID: r.ID, Model: r.Model}
	if u := r.Usage; u != nil {
		res.Usage = &wireUsage{InputTokens: u.PromptTokens, OutputTokens: u.CompletionTokens}
		if d := u.CompletionTokensDetails; d != nil {
			res.Usage.ReasoningTokens = d.ReasoningTokens
		}
	}
	if r.Error != nil && r.Error.Message != "" {
		return res, semconv.ErrorTypeOther.Value.AsString(), fmt.Errorf("%w: %s", errEndpoint, scrub(clip(r.Error.Message, 256)))
	}
	if len(r.Choices) == 0 {
		return res, "no_choices", fmt.Errorf("jev: %w: no choices", llmio.ErrMalformed)
	}
	for _, c := range r.Choices {
		res.FinishReasons = append(res.FinishReasons, c.FinishReason)
	}
	c := r.Choices[0]
	if c.Message.Content != nil {
		res.Text = *c.Message.Content
	}
	if strings.TrimSpace(c.Message.ReasoningContent) != "" {
		res.Reasoning = []string{c.Message.ReasoningContent}
	}
	res.OutputFinishReason = outputFinishReason(c.FinishReason)

	errType := ""
	switch {
	case c.Message.Refusal != "":
		errType = "refusal"
	case c.FinishReason == "stop" || c.FinishReason == "":
	case c.FinishReason == "length":
		errType = "max_tokens"
	default:
		errType = "finish_reason_" + c.FinishReason
	}
	if errType != "" {
		return res, errType, fmt.Errorf("%w: jev: %s", decide.ErrInvalid, errType)
	}

	resp, err := llmio.Parse(req, []byte(res.Text), Name)
	if err != nil {
		return res, "invalid_output", fmt.Errorf("jev: %w", err)
	}
	res.Response = resp
	return res, "", nil
}

// outputFinishReason maps the (assumed OpenAI) finish_reason onto the semconv
// enum. TODO(jev-api): Jev's own finish/stop reasons.
func outputFinishReason(r string) string {
	switch r {
	case "stop", "":
		return llmio.FinishStop
	case "length":
		return llmio.FinishLength
	case "content_filter":
		return llmio.FinishContentFilter
	default:
		return r
	}
}
