package decision

import (
	"context"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// context_note_test.go drives the M7-3 operator context note (#930) end-to-end through
// the decision paths. It mirrors context_window_test.go (#929): the #739 inferBackend
// fake records lastPromptSystem so we can prove the validated note reaches the model's
// System prompt on BOTH the sync path (llmDecideLoop) and the async PRODUCTION path
// (asyncLoop -> AwaitInflight), that an invalid/oversized note is rejected before it
// reaches the model, that the base facts remain present + authoritative, and that the
// present/len span attributes are stamped on every llm call.

// TestContextNote_ReachesPrompt_Sync: a valid note set on the live snapshot is injected
// into the System prompt on the synchronous llm decision path, the base facts still
// precede it, and the inference span carries present=true + the rune length.
func TestContextNote_ReachesPrompt_Sync(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	note := "Crowd skews older tonight; keep the pacing gentle."
	snap.PromptContextNote = note

	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}
	if !strings.Contains(be.lastPromptSystem, note) {
		t.Errorf("validated note must reach the System prompt:\n%s", be.lastPromptSystem)
	}
	if !strings.Contains(be.lastPromptSystem, "OPERATOR CONTEXT") {
		t.Errorf("operator section header missing from prompt:\n%s", be.lastPromptSystem)
	}
	// Base-facts authority (#930): the role line is present AND precedes the note.
	roleIdx := strings.Index(be.lastPromptSystem, "You are the JoustMania game director")
	noteIdx := strings.Index(be.lastPromptSystem, note)
	if roleIdx < 0 {
		t.Fatalf("base facts (role) missing — base must remain hardcoded:\n%s", be.lastPromptSystem)
	}
	if roleIdx >= noteIdx {
		t.Errorf("base facts (idx %d) must precede the operator note (idx %d)", roleIdx, noteIdx)
	}

	present, length := notePresenceFromInfer(t, sr)
	if !present {
		t.Errorf("%s = false, want true (note injected)", AttrLLMContextNotePresent)
	}
	if length != int64(len([]rune(note))) {
		t.Errorf("%s = %d, want %d", AttrLLMContextNoteLen, length, len([]rune(note)))
	}
}

// TestContextNote_RejectedNotInjected_Sync: an oversized note and an empty note are
// REJECTED before reaching the model — the prompt carries no operator section and the
// span reports present=false/len=0, identical to the no-note case.
func TestContextNote_RejectedNotInjected_Sync(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"oversized", strings.Repeat("x", llm.MaxContextNoteLen+1)},
		{"empty", ""},
		{"whitespace only", "   \n  "},
		{"control char", "bad\x00note"},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
			snap := llmDecideSnapshot()
			snap.PromptContextNote = tc.raw

			l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)
			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

			if strings.Contains(be.lastPromptSystem, "OPERATOR CONTEXT") {
				t.Errorf("rejected note must add no operator section:\n%s", be.lastPromptSystem)
			}
			// The oversized raw text must not appear verbatim either.
			if tc.raw != "" && strings.TrimSpace(tc.raw) != "" && strings.Contains(be.lastPromptSystem, tc.raw) {
				t.Errorf("rejected note leaked into the prompt:\n%s", be.lastPromptSystem)
			}
			// Base facts always present (unaffected by a rejected note).
			if !strings.Contains(be.lastPromptSystem, "You are the JoustMania game director") {
				t.Errorf("base facts missing on the rejected-note path:\n%s", be.lastPromptSystem)
			}
			present, length := notePresenceFromInfer(t, sr)
			if present || length != 0 {
				t.Errorf("rejected note span = (present=%v len=%d), want (false, 0)", present, length)
			}
		})
	}
}

// TestContextNote_FlagIsLive: flipping prompt.context_note on the snapshot changes the
// very next llm call's prompt — no restart (mirrors the #929 live-flag test).
func TestContextNote_FlagIsLive(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	fl := &settableFlags{snap: llmDecideSnapshot()}

	l, _, _ := llmDecideLoop(t, fl.snap, resolverWith(be), nil)
	l.Flags = fl

	// First call: no note set -> no operator section.
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	if strings.Contains(be.lastPromptSystem, "OPERATOR CONTEXT") {
		t.Fatalf("unset note should inject no operator section:\n%s", be.lastPromptSystem)
	}

	// Set the note LIVE — honored on the next call with no restart.
	fl.snap.PromptContextNote = "Keep the energy high — competitive crowd."
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())
	if !strings.Contains(be.lastPromptSystem, "Keep the energy high") {
		t.Errorf("live note change not honored on the next call:\n%s", be.lastPromptSystem)
	}
}

// TestContextNote_CaptureSpanCarriesPresence: the capture path (agent.llm.prompt) also
// injects the validated note and stamps present/len — the cheap-on-capture third site.
func TestContextNote_CaptureSpanCarriesPresence(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	note := "Spectators are loud; bias toward audio cues."
	snap.PromptContextNote = note

	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), nil)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	caps := spansByName(sr.Ended(), SpanLLMPrompt)
	if len(caps) != 1 {
		t.Fatalf("agent.llm.prompt spans = %d, want 1", len(caps))
	}
	if v, ok := attrValue(caps[0], AttrLLMContextNotePresent); !ok || !v.AsBool() {
		t.Errorf("capture span %s = %v (present=%v), want true", AttrLLMContextNotePresent, v.AsBool(), ok)
	}
	if v, ok := attrValue(caps[0], AttrLLMContextNoteLen); !ok || v.AsInt64() != int64(len([]rune(note))) {
		t.Errorf("capture span %s = %d, want %d", AttrLLMContextNoteLen, v.AsInt64(), len([]rune(note)))
	}
	// #1168: the System text no longer rides the span (only its fingerprint), so the
	// note's PRESENCE in the prompt is asserted via the present/len attrs above; the
	// note's TEXT reaching the System prompt is covered by the production-path tests
	// (the backend.lastPromptSystem assertions). Here we just confirm the span carries
	// the System-prompt fingerprint instead of the full text.
	if v, ok := attrValue(caps[0], AttrLLMPromptSystemSHA); !ok || v.AsString() == "" {
		t.Errorf("capture span missing the System-prompt fingerprint (llm.prompt.system_sha256)")
	}
}

// TestContextNote_AsyncPathInjectsNote is the PRODUCTION (async, #917) coverage: a loop
// wired with a context provider must carry the validated note in the System prompt the
// backend receives AND stamp present/len on the agent.llm.infer span at apply time.
// Without runInfer's resolveContextNote call the feature would be bypassed in
// production — this proves it isn't (mirrors TestContextWindow_AsyncPathInjectsBlock).
func TestContextNote_AsyncPathInjectsNote(t *testing.T) {
	be := newRecordingBlockingBackend("phi4-mini", validShieldResponse)
	snap := asyncSnapshot()
	note := "Tournament finals — heightened stakes, lean competitive."
	snap.PromptContextNote = note

	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("async Infer never started — the loop did not fire on the async path")
	}
	close(be.release)
	l.AwaitInflight()

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}
	got := be.systemPrompt()
	if !strings.Contains(got, note) {
		t.Errorf("async production prompt missing the operator note:\n%s", got)
	}
	if !strings.Contains(got, "OPERATOR CONTEXT") {
		t.Errorf("async production prompt missing the operator section:\n%s", got)
	}
	// Base facts still authoritative + present on the async path.
	if !strings.Contains(got, "You are the JoustMania game director") {
		t.Errorf("base facts missing on the async path:\n%s", got)
	}

	present, length := notePresenceFromInfer(t, sr)
	if !present {
		t.Errorf("async infer span %s = false, want true", AttrLLMContextNotePresent)
	}
	if length != int64(len([]rune(note))) {
		t.Errorf("async infer span %s = %d, want %d", AttrLLMContextNoteLen, length, len([]rune(note)))
	}
}

// TestContextNote_AsyncRejectedNotInjected: an oversized note is rejected on the async
// production path too — no operator section, span present=false/len=0.
func TestContextNote_AsyncRejectedNotInjected(t *testing.T) {
	be := newRecordingBlockingBackend("phi4-mini", validShieldResponse)
	snap := asyncSnapshot()
	snap.PromptContextNote = strings.Repeat("z", llm.MaxContextNoteLen+1)

	provider := newFakeContextProvider()
	provider.set("g1", activeContext("g1", "AAAA"))
	l, sr, _ := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), activeContext("g1", "AAAA"), testTrigger())
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("async Infer never started")
	}
	close(be.release)
	l.AwaitInflight()

	if strings.Contains(be.systemPrompt(), "OPERATOR CONTEXT") {
		t.Errorf("oversized note must inject no operator section on the async path:\n%s", be.systemPrompt())
	}
	present, length := notePresenceFromInfer(t, sr)
	if present || length != 0 {
		t.Errorf("async rejected-note span = (present=%v len=%d), want (false, 0)", present, length)
	}
}

// notePresenceFromInfer reads the MOST RECENT agent.llm.infer span's M7-3 operator-note
// view (present + rune length). Mirrors lastInjectedCount for #929.
func notePresenceFromInfer(t *testing.T, sr *tracetest.SpanRecorder) (present bool, length int64) {
	t.Helper()
	infs := spansByName(sr.Ended(), SpanLLMInfer)
	if len(infs) == 0 {
		t.Fatalf("no agent.llm.infer span recorded")
	}
	span := infs[len(infs)-1]
	pv, ok := attrValue(span, AttrLLMContextNotePresent)
	if !ok {
		t.Fatalf("agent.llm.infer span missing %s", AttrLLMContextNotePresent)
	}
	lv, ok := attrValue(span, AttrLLMContextNoteLen)
	if !ok {
		t.Fatalf("agent.llm.infer span missing %s", AttrLLMContextNoteLen)
	}
	return pv.AsBool(), lv.AsInt64()
}
