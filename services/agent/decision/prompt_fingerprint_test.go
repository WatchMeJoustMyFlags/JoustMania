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
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
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
	if !ref.emit(log, hashA, "balanced", "llm", "noop", sysA) {
		t.Fatal("first sight of a hash must emit")
	}
	if ref.emit(log, hashA, "balanced", "llm", "noop", sysA) {
		t.Error("second sight of the SAME hash must NOT re-emit")
	}
	const sysB = "system prompt B (different variant rendered)"
	if !ref.emit(log, systemPromptSHA(sysB), "aggressive", "llm", "noop", sysB) {
		t.Error("a NEW hash must emit (system prompt changed)")
	}
}

// recordingLoopWithLog mirrors recordingLoop but threads a capturing JSON logger so
// the test can read the agent.llm.system_prompt reference line back.
func recordingLoopWithLog(t *testing.T, snap flags.Snapshot, buf *bytes.Buffer) (*Loop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewJSONHandler(buf, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: nil}
	l.Actions = &fakeSink{}
	l.SetLLMBudget(NewLLMBudget())
	return l, sr
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

// TestLLMCapture_ReferenceLogCarriesFullSystemOncePerFingerprint: the in-game
// capture path emits the full System text on the reference LOG exactly once per
// fingerprint, and the span carries the matching hash (not the text).
func TestLLMCapture_ReferenceLogCarriesFullSystemOncePerFingerprint(t *testing.T) {
	resetSystemPromptRef()
	t.Cleanup(resetSystemPromptRef)

	var buf bytes.Buffer
	l, sr := recordingLoopWithLog(t, llmSnapshot(), &buf)
	clock := time.Unix(1000, 0)
	l.now = func() time.Time { return clock }

	// Two cycles, same snapshot -> same system prompt -> one reference line. Advance
	// past the 1s throttle so BOTH cycles actually build+emit a capture span.
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	clock = clock.Add(2 * time.Second)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	caps := spansByName(sr.Ended(), SpanLLMPrompt)
	if len(caps) != 2 {
		t.Fatalf("agent.llm.prompt spans = %d, want 2 (throttle elapsed between cycles)", len(caps))
	}

	refs := systemRefLines(t, &buf)
	if len(refs) != 1 {
		t.Fatalf("agent.llm.system_prompt reference lines = %d, want 1 (once per fingerprint)", len(refs))
	}
	ref := refs[0]
	// The reference carries the FULL system text + the determinants + the hash.
	full, _ := ref["system"].(string)
	if full == "" || !strings.Contains(full, "RESPONSE CONTRACT") {
		t.Errorf("reference line must carry the full system text, got %q", full)
	}
	refHash, _ := ref["system_sha256"].(string)
	if refHash == "" || systemPromptSHA(full) != refHash {
		t.Errorf("reference hash %q must match sha of its own system text", refHash)
	}
	// The span's fingerprint matches the reference's hash, and the span does NOT carry
	// the large physics/allow-list system text.
	spanHash, ok := attrValue(caps[0], AttrLLMPromptSystemSHA)
	if !ok || spanHash.AsString() != refHash {
		t.Errorf("span hash %q must match reference hash %q", spanHash.AsString(), refHash)
	}
	if _, ok := attrValue(caps[0], "llm.prompt.system"); ok {
		t.Error("span must NOT carry the full system text (only the fingerprint)")
	}
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
