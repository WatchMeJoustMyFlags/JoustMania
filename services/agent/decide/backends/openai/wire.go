package openai

import (
	"bytes"
	"encoding/json"
	"strings"

	"github.com/joustmania/agent/decide/llmio"
)

// Response formats, in preference order. json_schema is the strict structured
// output; json_object (valid JSON, shape described only by the prompt) is the
// fallback for vendors that reject json_schema with a 400.
const (
	formatJSONSchema = "json_schema"
	formatJSONObject = "json_object"
)

// schemaName is response_format.json_schema.name.
const schemaName = "joustmania_decision"

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type jsonSchemaFormat struct {
	Name   string         `json:"name"`
	Strict bool           `json:"strict"`
	Schema map[string]any `json:"schema"`
}

type responseFormat struct {
	Type       string            `json:"type"`
	JSONSchema *jsonSchemaFormat `json:"json_schema,omitempty"`
}

// chatRequest is the /chat/completions body. Optional fields are omitted when
// unset so a vendor that rejects a parameter for a model (OpenAI reasoning
// models reject temperature != 1 and max_tokens) never sees it.
type chatRequest struct {
	Model               string         `json:"model"`
	Messages            []chatMessage  `json:"messages"`
	ResponseFormat      responseFormat `json:"response_format"`
	MaxTokens           int            `json:"max_tokens,omitempty"`
	MaxCompletionTokens int            `json:"max_completion_tokens,omitempty"`
	Temperature         *float64       `json:"temperature,omitempty"`
	ReasoningEffort     string         `json:"reasoning_effort,omitempty"`
	Stream              bool           `json:"stream"`
}

// messageContent accepts the two content shapes seen across vendors: a plain
// string (the OpenAI shape) and an array of typed parts (Mistral's reasoning
// models return [{"type":"thinking",...},{"type":"text","text":...}]).
type messageContent struct {
	text     string
	thinking []string
}

func (c *messageContent) UnmarshalJSON(b []byte) error {
	b = bytes.TrimSpace(b)
	if len(b) == 0 || bytes.Equal(b, []byte("null")) {
		return nil
	}
	if b[0] == '"' {
		return json.Unmarshal(b, &c.text)
	}
	var parts []struct {
		Type     string          `json:"type"`
		Text     string          `json:"text"`
		Thinking json.RawMessage `json:"thinking"`
	}
	if err := json.Unmarshal(b, &parts); err != nil {
		return err
	}
	var text strings.Builder
	for _, p := range parts {
		switch p.Type {
		case "text":
			text.WriteString(p.Text)
		case "thinking", "reasoning":
			if t := thinkingText(p.Thinking, p.Text); t != "" {
				c.thinking = append(c.thinking, t)
			}
		}
	}
	c.text = text.String()
	return nil
}

// thinkingText reads a thinking part, which is a string or (Mistral) an array
// of {"type":"text","text":...}.
func thinkingText(raw json.RawMessage, fallback string) string {
	var s string
	if json.Unmarshal(raw, &s) == nil && s != "" {
		return s
	}
	var parts []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) == nil {
		var b strings.Builder
		for _, p := range parts {
			b.WriteString(p.Text)
		}
		if b.Len() > 0 {
			return b.String()
		}
	}
	return fallback
}

type chatChoice struct {
	Message struct {
		Content messageContent `json:"content"`
		// Reasoning text: DeepSeek/Qwen/vLLM use reasoning_content, OpenRouter
		// and Groq use reasoning.
		ReasoningContent string `json:"reasoning_content"`
		Reasoning        string `json:"reasoning"`
		Refusal          string `json:"refusal"`
	} `json:"message"`
	FinishReason string `json:"finish_reason"`
}

type chatUsage struct {
	PromptTokens        int `json:"prompt_tokens"`
	CompletionTokens    int `json:"completion_tokens"`
	PromptTokensDetails *struct {
		CachedTokens int `json:"cached_tokens"`
	} `json:"prompt_tokens_details"`
	CompletionTokensDetails *struct {
		ReasoningTokens int `json:"reasoning_tokens"`
	} `json:"completion_tokens_details"`
}

type chatResponse struct {
	ID      string       `json:"id"`
	Model   string       `json:"model"`
	Choices []chatChoice `json:"choices"`
	Usage   *chatUsage   `json:"usage"`
	// Error is the OpenAI error envelope some servers return with a 200.
	Error *struct {
		Message string `json:"message"`
	} `json:"error"`
}

// reasoning collects the non-empty reasoning texts of a choice.
func (c chatChoice) reasoning() []string {
	out := append([]string(nil), c.Message.Content.thinking...)
	for _, r := range []string{c.Message.ReasoningContent, c.Message.Reasoning} {
		if strings.TrimSpace(r) != "" {
			out = append(out, r)
		}
	}
	return out
}

// finishReason maps an OpenAI finish_reason onto the semconv output-message
// finish_reason enum.
func finishReason(r string) string {
	switch r {
	case "stop", "":
		return llmio.FinishStop
	case "length":
		return llmio.FinishLength
	case "content_filter":
		return llmio.FinishContentFilter
	case "tool_calls", "function_call":
		return llmio.FinishToolCall
	default:
		return r
	}
}
