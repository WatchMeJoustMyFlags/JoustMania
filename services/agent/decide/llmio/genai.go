package llmio

import (
	"encoding/json"

	"go.opentelemetry.io/otel/attribute"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
)

// OTel GenAI semantic-convention message format (semconv v1.41.0,
// gen-ai-input-messages.json / gen-ai-output-messages.json /
// gen-ai-system-instructions.json). Spans record these as JSON strings, which
// the spec allows when structured attributes are not supported. They carry
// prompt and model text, so backends record them ONLY when content capture is
// opted into (the spec's "opt-in" requirement level).

// Part types used by the decision backends.
const (
	PartText      = "text"
	PartReasoning = "reasoning"
)

// Roles used by the decision backends.
const (
	RoleUser      = "user"
	RoleAssistant = "assistant"
)

// Output-message finish reasons from the semconv enum.
const (
	FinishStop          = "stop"
	FinishLength        = "length"
	FinishContentFilter = "content_filter"
	FinishToolCall      = "tool_call"
	FinishError         = "error"
)

// MaxPartContentBytes caps one part's content in a captured attribute. The
// spec lets instrumentations truncate content; capping each part (rather than
// the serialized attribute) keeps the attribute valid JSON.
const MaxPartContentBytes = 16 << 10

// Part is a semconv message part: {"type": "text"|"reasoning", "content": ...}.
type Part struct {
	Type    string `json:"type"`
	Content string `json:"content"`
}

// Message is a semconv chat message. FinishReason is set on output messages
// only.
type Message struct {
	Role         string `json:"role"`
	Parts        []Part `json:"parts"`
	FinishReason string `json:"finish_reason,omitempty"`
}

// TextPart is a "text" part.
func TextPart(s string) Part { return Part{Type: PartText, Content: capBytes(s)} }

// ReasoningPart is a "reasoning" part: model thinking, or the model's own
// rationale for its answer, so tracing UIs show the "why" in the standard place.
func ReasoningPart(s string) Part { return Part{Type: PartReasoning, Content: capBytes(s)} }

// OutputMessage builds the assistant output message of one structured-answer
// call. Parts, in order: one reasoning part per non-empty thinking block, one
// reasoning part for the parsed rationale (when non-empty), then the raw model
// text. finishReason should be one of the Finish* constants.
func OutputMessage(text string, thinking []string, rationale, finishReason string) Message {
	parts := make([]Part, 0, len(thinking)+2)
	for _, t := range thinking {
		if t != "" {
			parts = append(parts, ReasoningPart(t))
		}
	}
	if rationale != "" {
		parts = append(parts, ReasoningPart(rationale))
	}
	if text != "" {
		parts = append(parts, TextPart(text))
	}
	return Message{Role: RoleAssistant, Parts: parts, FinishReason: finishReason}
}

// SystemInstructionsJSON is the gen_ai.system_instructions value for p: an
// array of parts.
func SystemInstructionsJSON(p Prompt) string {
	return marshal([]Part{TextPart(p.System)})
}

// InputMessagesJSON is the gen_ai.input.messages value for p: the single user
// message (the system prompt goes to gen_ai.system_instructions, as the spec
// requires for providers that take it separately).
func InputMessagesJSON(p Prompt) string {
	return marshal([]Message{{Role: RoleUser, Parts: []Part{TextPart(p.User)}}})
}

// OutputMessagesJSON is the gen_ai.output.messages value.
func OutputMessagesJSON(msgs ...Message) string {
	if msgs == nil {
		msgs = []Message{}
	}
	for i := range msgs {
		if msgs[i].Parts == nil {
			msgs[i].Parts = []Part{}
		}
	}
	return marshal(msgs)
}

// InputContentAttributes are the opt-in content attributes known before the
// call: gen_ai.system_instructions and gen_ai.input.messages.
func InputContentAttributes(p Prompt) []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.GenAISystemInstructionsKey.String(SystemInstructionsJSON(p)),
		semconv.GenAIInputMessagesKey.String(InputMessagesJSON(p)),
	}
}

// OutputContentAttribute is the opt-in gen_ai.output.messages attribute.
func OutputContentAttribute(msgs ...Message) attribute.KeyValue {
	return semconv.GenAIOutputMessagesKey.String(OutputMessagesJSON(msgs...))
}

func marshal(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		// Parts and messages are plain strings; Marshal cannot fail on them.
		return "[]"
	}
	return string(b)
}

// capBytes truncates s to MaxPartContentBytes on a rune boundary.
func capBytes(s string) string {
	if len(s) <= MaxPartContentBytes {
		return s
	}
	cut := MaxPartContentBytes
	for cut > 0 && !runeStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…[truncated]"
}

func runeStart(b byte) bool { return b&0xC0 != 0x80 }
