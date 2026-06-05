package decision

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// llmSnapshot is a representative llm-mode snapshot for the capture tests: a
// model + variant, objectives, and a permissive allow-list so the rules path is
// not the variable under test.
func llmSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:    true,
		Mode:       "llm",
		Objectives: map[string]float64{"endurance": 0.7, "chaos": 0.3},
		Capability: flags.Capability{Model: "phi4-mini", PromptVariant: "balanced"},
		InterventionsAllowed: []string{
			"noop", "grant_shield", "play_audio_cue",
		},
		Policy: flags.Policy{MaxInterventionsPerMinute: 100},
	}
}

// TestLLMCapture_OnIdle: mode=llm and the rules engine returns zero decisions —
// the lazy agent.decision span never emits, but the dedicated agent.llm.prompt
// span MUST exist with non-empty prompt text and the fallback attribution.
func TestLLMCapture_OnIdle(t *testing.T) {
	l, sr := recordingLoop(t, llmSnapshot(), nil)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	// No decision was produced, so no decision trace.
	if n := len(spansByName(sr.Ended(), SpanDecision)); n != 0 {
		t.Fatalf("agent.decision spans = %d, want 0 on an idle cycle", n)
	}
	caps := spansByName(sr.Ended(), SpanLLMPrompt)
	if len(caps) != 1 {
		t.Fatalf("agent.llm.prompt spans = %d, want 1", len(caps))
	}
	capt := caps[0]
	if v, ok := attrValue(capt, AttrLLMPromptSystem); !ok || v.AsString() == "" {
		t.Error("llm.prompt.system must be present and non-empty")
	}
	if v, ok := attrValue(capt, AttrLLMPromptUser); !ok || v.AsString() == "" {
		t.Error("llm.prompt.user must be present and non-empty")
	}
	if v, ok := attrValue(capt, AttrInferenceFallback); !ok || v.AsString() != FallbackNoBackend {
		t.Errorf("inference.fallback_reason = %q, want %q", v.AsString(), FallbackNoBackend)
	}
	if v, ok := attrValue(capt, AttrInferenceUsed); !ok || v.AsString() != InferenceRules {
		t.Errorf("inference.used = %q, want %q", v.AsString(), InferenceRules)
	}
}

// TestLLMCapture_SchemaComplete: every attribute of the agent.llm.prompt schema
// is present on the span, with the values derived from the snapshot/prompt.
func TestLLMCapture_SchemaComplete(t *testing.T) {
	l, sr := recordingLoop(t, llmSnapshot(), nil)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	capt := spansByName(sr.Ended(), SpanLLMPrompt)[0]
	wantStr := map[string]string{
		"gen_ai.operation.name":  "chat",
		"gen_ai.request.model":   "phi4-mini",
		"gen_ai.output.type":     "json",
		AttrMode:                 "llm",
		AttrPromptVariant:        "balanced",
		AttrObjectives:           "chaos=0.3,endurance=0.7",
		AttrInterventionsAllowed: "noop,grant_shield,play_audio_cue",
		AttrInferenceConfigured:  "phi4-mini",
		AttrInferenceUsed:        InferenceRules,
		AttrInferenceFallback:    FallbackNoBackend,
	}
	for key, expected := range wantStr {
		if v, ok := attrValue(capt, key); !ok || v.AsString() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
	// llm.prompt.bytes equals the combined length of the two prompt texts.
	sys, _ := attrValue(capt, AttrLLMPromptSystem)
	usr, _ := attrValue(capt, AttrLLMPromptUser)
	wantBytes := int64(len(sys.AsString()) + len(usr.AsString()))
	if v, ok := attrValue(capt, AttrLLMPromptBytes); !ok || v.AsInt64() != wantBytes {
		t.Errorf("llm.prompt.bytes = %d, want %d", v.AsInt64(), wantBytes)
	}
}

// TestLLMCapture_FallbackStillHappens: mode=llm and the rules engine DOES return
// a decision — both the capture span AND the normal agent.decision span must
// exist, and the decision must have come from the rules engine (the action sink
// was called).
func TestLLMCapture_FallbackStillHappens(t *testing.T) {
	l, sr := recordingLoop(t, llmSnapshot(), []Decision{{
		Intervention: "grant_shield",
		Reason:       "from rules",
	}})
	sink := &fakeSink{}
	l.Actions = sink

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 1 {
		t.Errorf("agent.llm.prompt spans = %d, want 1", n)
	}
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (rules fallback)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "from rules" {
		t.Errorf("decision.reason = %q, want \"from rules\" (came from rules engine)", v.AsString())
	}
	// The decision span records the same fallback attribution as the capture span.
	if v, ok := attrValue(decs[0], AttrInferenceFallback); !ok || v.AsString() != FallbackNoBackend {
		t.Errorf("decision inference.fallback_reason = %q, want %q", v.AsString(), FallbackNoBackend)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules decision dispatched)", sink.calls.Load())
	}
}

// TestLLMCapture_Throttled: rapid llm cycles emit at most one capture span per
// throttle interval. The recording loop uses the default 1s throttle and a real
// clock, so 20 back-to-back cycles fall inside one interval.
func TestLLMCapture_Throttled(t *testing.T) {
	l, sr := recordingLoop(t, llmSnapshot(), nil)

	for i := 0; i < 20; i++ {
		l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())
	}
	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 1 {
		t.Errorf("agent.llm.prompt spans = %d, want 1 (throttled)", n)
	}
}

// TestLLMCapture_ThrottleReopens: once the throttle interval elapses, a new llm
// cycle emits a second capture span. Drives an injected clock past the interval.
func TestLLMCapture_ThrottleReopens(t *testing.T) {
	l, sr := recordingLoop(t, llmSnapshot(), nil)
	clock := time.Unix(1000, 0)
	l.now = func() time.Time { return clock }

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())
	clock = clock.Add(2 * time.Second) // past the 1s default throttle
	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 2 {
		t.Errorf("agent.llm.prompt spans = %d, want 2 (interval elapsed)", n)
	}
}

// TestLLMCapture_RulesModeNoCapture: mode=rules emits NO capture span — the
// prompt-capture path is llm-mode-only.
func TestLLMCapture_RulesModeNoCapture(t *testing.T) {
	snap := llmSnapshot()
	snap.Mode = "rules"
	l, sr := recordingLoop(t, snap, []Decision{{Intervention: "grant_shield", Reason: "x"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	if n := len(spansByName(sr.Ended(), SpanLLMPrompt)); n != 0 {
		t.Errorf("agent.llm.prompt spans = %d, want 0 in rules mode", n)
	}
}
