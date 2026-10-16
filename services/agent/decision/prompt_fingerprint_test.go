package decision

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// TestSystemPromptSHA_Stable: the fingerprint is the first systemPromptSHALen hex
// chars of the full SHA-256 digest, and identical input always yields identical
// output (so the reference log is resolvable for any span carrying the hash).
func TestSystemPromptSHA_Stable(t *testing.T) {
	const s = "the large constant system prompt"
	got := systemPromptSHA(s)
	if len(got) != systemPromptSHALen {
		t.Fatalf("hash len = %d, want %d", len(got), systemPromptSHALen)
	}
	want := fmt.Sprintf("%x", sha256.Sum256([]byte(s)))[:systemPromptSHALen]
	if got != want {
		t.Errorf("hash = %q, want %q (prefix of full sha256)", got, want)
	}
	if systemPromptSHA(s) != got {
		t.Error("hash is not stable for identical input")
	}
	if systemPromptSHA(s+"x") == got {
		t.Error("a different system prompt must hash differently")
	}
}

// TestCapUserPrompt: a user prompt under the bound is untouched; one over the bound
// is cut to the bound and carries the truncation marker, preserving the content.
func TestCapUserPrompt(t *testing.T) {
	small := strings.Repeat("u", 100)
	if got := capUserPrompt(small); got != small {
		t.Error("a small user prompt must be returned unchanged")
	}
	big := strings.Repeat("u", maxUserPromptBytes+500)
	got := capUserPrompt(big)
	if !strings.HasSuffix(got, userPromptTruncatedMarker) {
		t.Error("an oversized user prompt must carry the truncation marker")
	}
	if !strings.HasPrefix(got, big[:maxUserPromptBytes]) {
		t.Error("the capped user prompt must preserve content up to the bound")
	}
	if len(got) != maxUserPromptBytes+len(userPromptTruncatedMarker) {
		t.Errorf("capped len = %d, want %d", len(got), maxUserPromptBytes+len(userPromptTruncatedMarker))
	}
}

// TestSystemPromptReference_EmitsOncePerFingerprint: the full text is logged on the
// first sight of a hash and NOT re-logged on a second identical sight; a new hash
// (changed system prompt) re-emits.
func TestSystemPromptReference_EmitsOncePerFingerprint(t *testing.T) {
	ref := &systemPromptReference{}
	log := slog.New(slog.NewJSONHandler(&bytes.Buffer{}, nil))

	const sysA = "system prompt A"
	hashA := systemPromptSHA(sysA)
	if !ref.emit(log, hashA, "retro", "noop", sysA) {
		t.Fatal("first sight of a hash must emit")
	}
	if ref.emit(log, hashA, "retro", "noop", sysA) {
		t.Error("second sight of the SAME hash must NOT re-emit")
	}
	const sysB = "system prompt B (different allow-list rendered)"
	if !ref.emit(log, systemPromptSHA(sysB), "retro", "noop", sysB) {
		t.Error("a NEW hash must emit (system prompt changed)")
	}
}

// systemRefLines returns every agent.llm.system_prompt reference log line captured
// in buf, decoded as a map.
func systemRefLines(t *testing.T, buf *bytes.Buffer) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, line := range strings.Split(strings.TrimSpace(buf.String()), "\n") {
		if line == "" {
			continue
		}
		var m map[string]any
		if err := json.Unmarshal([]byte(line), &m); err != nil {
			t.Fatalf("log line not JSON: %q: %v", line, err)
		}
		if m["msg"] == SpanLLMSystemPromptRef {
			out = append(out, m)
		}
	}
	return out
}

// TestRetroCapture_ReferenceLogCarriesFullSystemOnce: the retro path also emits the
// full System text once per fingerprint, and the retro span carries the hash.
func TestRetroCapture_ReferenceLogCarriesFullSystemOnce(t *testing.T) {
	resetSystemPromptRef()
	t.Cleanup(resetSystemPromptRef)

	var buf bytes.Buffer
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	rc := NewRetroCoordinator(&settableFlags{snap: retroFlagSnapshot()}, slog.New(slog.NewJSONHandler(&buf, nil)))
	rc.Tracer = tp.Tracer("test")

	// Two distinct sessions, same flags -> same retro system prompt -> one reference.
	s1 := endedSession()
	s1.SessionID = "session-A"
	s2 := endedSession()
	s2.SessionID = "session-B"
	rc.OnGameEnd(s1)
	rc.OnGameEnd(s2)

	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("agent.llm.retro spans = %d, want 2", n)
	}
	refs := systemRefLines(t, &buf)
	if len(refs) != 1 {
		t.Fatalf("agent.llm.system_prompt reference lines = %d, want 1 (once per fingerprint)", len(refs))
	}
	if mode, _ := refs[0]["mode"].(string); mode != retroModeValue {
		t.Errorf("reference mode = %q, want %q", mode, retroModeValue)
	}
	span := spansByName(sr.Ended(), SpanLLMRetro)[0]
	refHash, _ := refs[0]["system_sha256"].(string)
	if h, ok := attrValue(span, AttrLLMRetroSystemSHA); !ok || h.AsString() != refHash {
		t.Errorf("retro span hash %q must match reference hash %q", h.AsString(), refHash)
	}
}
