package llmio

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestGenAIMessageFormat(t *testing.T) {
	p := Prompt{System: "sys", User: "usr"}

	var sys []Part
	if err := json.Unmarshal([]byte(SystemInstructionsJSON(p)), &sys); err != nil {
		t.Fatal(err)
	}
	if len(sys) != 1 || sys[0] != (Part{Type: "text", Content: "sys"}) {
		t.Errorf("system_instructions = %+v", sys)
	}

	var in []Message
	if err := json.Unmarshal([]byte(InputMessagesJSON(p)), &in); err != nil {
		t.Fatal(err)
	}
	if len(in) != 1 || in[0].Role != "user" || in[0].Parts[0].Content != "usr" || in[0].FinishReason != "" {
		t.Errorf("input.messages = %+v", in)
	}
	if strings.Contains(InputMessagesJSON(p), "finish_reason") {
		t.Error("input messages must not carry finish_reason")
	}

	out := OutputMessage(`{"answers":[]}`, []string{"thought", ""}, "because", FinishStop)
	var msgs []Message
	if err := json.Unmarshal([]byte(OutputMessagesJSON(out)), &msgs); err != nil {
		t.Fatal(err)
	}
	want := []Part{
		{Type: "reasoning", Content: "thought"},
		{Type: "reasoning", Content: "because"},
		{Type: "text", Content: `{"answers":[]}`},
	}
	if len(msgs) != 1 || msgs[0].Role != "assistant" || msgs[0].FinishReason != "stop" {
		t.Fatalf("output.messages = %+v", msgs)
	}
	for i, w := range want {
		if msgs[0].Parts[i] != w {
			t.Errorf("part %d = %+v, want %+v", i, msgs[0].Parts[i], w)
		}
	}

	// An empty output still serializes parts as [] (the schema requires the array).
	if got := OutputMessagesJSON(OutputMessage("", nil, "", FinishError)); !strings.Contains(got, `"parts":[]`) {
		t.Errorf("empty output = %s", got)
	}
}

func TestGenAIContentCapped(t *testing.T) {
	big := strings.Repeat("é", MaxPartContentBytes) // 2 bytes per rune
	part := TextPart(big)
	if len(part.Content) > MaxPartContentBytes+len("…[truncated]") {
		t.Errorf("content not capped: %d bytes", len(part.Content))
	}
	if !json.Valid([]byte(OutputMessagesJSON(OutputMessage(big, nil, "", FinishStop)))) {
		t.Error("capped content is not valid JSON")
	}
	if !strings.HasSuffix(part.Content, "[truncated]") {
		t.Error("truncation not marked")
	}
}

func TestContentAttributes(t *testing.T) {
	attrs := InputContentAttributes(Prompt{System: "s", User: "u"})
	keys := map[string]bool{}
	for _, a := range attrs {
		keys[string(a.Key)] = true
	}
	if !keys["gen_ai.system_instructions"] || !keys["gen_ai.input.messages"] {
		t.Errorf("keys = %v", keys)
	}
	if k := OutputContentAttribute(OutputMessage("x", nil, "", FinishStop)).Key; k != "gen_ai.output.messages" {
		t.Errorf("output key = %s", k)
	}
}
