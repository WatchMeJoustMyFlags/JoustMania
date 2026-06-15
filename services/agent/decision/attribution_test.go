package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// recordingLoop builds a Loop over the given flag snapshot with a recording
// tracer, a fixed-decision rules engine, and a no-op action sink. It is the
// #729 span-attribution fixture: drive a cycle, then read the spans back.
func recordingLoop(t *testing.T, snap flags.Snapshot, out []Decision) (*Loop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: out}
	l.Actions = &fakeSink{}
	// Inject a fresh global LLM budget (#847) so the gate's budget layer can admit
	// an llm-mode cycle; without it a nil budget would gate every llm attempt and
	// the capture-path tests (which assert the ALLOWED behavior) would never fire.
	l.SetLLMBudget(NewLLMBudget())
	return l, sr
}

// TestDecisionSpan_FullFlagSetLifted is the core #729 assertion: every flag
// evaluated this cycle appears on the agent.decision span. A single trace must
// answer which flags were in effect.
func TestDecisionSpan_FullFlagSetLifted(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:    true,
		Mode:       "rules",
		Objectives: map[string]float64{"endurance": 0.6, "chaos": 0.4},
		Capability: flags.Capability{Model: "gemma3:4b", PromptVariant: "balanced"},
		InterventionsAllowed: []string{
			"grant_shield", "play_audio_cue",
		},
		Policy: flags.Policy{
			BatteryThreshold:          25,
			MovementVarianceWindow:    12,
			MaxInterventionsPerMinute: 3,
		},
	}
	l, sr := recordingLoop(t, snap, []Decision{{
		Intervention:    "grant_shield",
		Reason:          "balance the field",
		ObjectiveServed: "balanced",
		Objectives:      map[string]float64{"endurance": 0.6, "chaos": 0.4},
	}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	dec := spansByName(sr.Ended(), SpanDecision)[0]

	wantStr := map[string]string{
		AttrEnabled:              "true",
		AttrGameKind:             "real",
		AttrMode:                 "rules",
		AttrObjectives:           "chaos=0.4,endurance=0.6",
		AttrModel:                "gemma3:4b",
		AttrPromptVariant:        "balanced",
		AttrInferenceConfigured:  "gemma3:4b",
		AttrInferenceUsed:        InferenceRules,
		AttrInferenceFallback:    "",
		AttrInterventionsAllowed: "grant_shield,play_audio_cue",
		AttrDecisionAction:       "grant_shield",
		AttrDecisionReason:       "balance the field",
		AttrDecisionObjective:    "balanced",
	}
	for key, expected := range wantStr {
		if v, ok := attrValue(dec, key); !ok || v.Emit() != expected {
			t.Errorf("attr %s = %q (present=%v), want %q", key, v.Emit(), ok, expected)
		}
	}
	wantInt := map[string]int64{
		AttrPolicyBatteryThreshold:       25,
		AttrPolicyMovementVarianceWindow: 12,
		AttrPolicyMaxPerMinute:           3,
	}
	for key, expected := range wantInt {
		if v, ok := attrValue(dec, key); !ok || v.AsInt64() != expected {
			t.Errorf("attr %s = %d (present=%v), want %d", key, v.AsInt64(), ok, expected)
		}
	}
	if v, ok := attrValue(dec, AttrDecisionBlocked); !ok || v.AsBool() {
		t.Errorf("decision.blocked = %v, want false", v.AsBool())
	}
}

// TestDecisionSpan_BlockedAttribution: a blocked decision carries
// decision.blocked=true and decision.block_reason on the span (#729 acceptance).
func TestDecisionSpan_BlockedAttribution(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"play_audio_cue"}, // eliminate_player not allowed
		Policy:               flags.Policy{MaxInterventionsPerMinute: 10},
	}
	l, sr := recordingLoop(t, snap, []Decision{{Intervention: "eliminate_player", Reason: "x"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	dec := spansByName(sr.Ended(), SpanDecision)[0]
	if v, ok := attrValue(dec, AttrDecisionBlocked); !ok || !v.AsBool() {
		t.Error("decision.blocked must be true")
	}
	if v, ok := attrValue(dec, AttrDecisionBlockReason); !ok || v.AsString() != string(ReasonNotAllowed) {
		t.Errorf("decision.block_reason = %q, want %q", v.AsString(), ReasonNotAllowed)
	}
	// game.kind is schema-complete: present as the empty string when unknown (#845).
	if v, ok := attrValue(dec, AttrGameKind); !ok || v.AsString() != "" {
		t.Errorf("game.kind = %q (present=%v), want empty string for unknown", v.AsString(), ok)
	}
}

// TestPerGameSpans_CarryGameID is the #1088 correlation assertion: every agent
// span that operates on a specific game carries the SAME game.id (and its
// session.id alias) as its agent.signal_received root, so a Jaeger query by
// game.id matches the whole per-game trace — not just the root — and correlates
// the agent's orphan trace with the coordinator's per-game trace. game.id IS the
// SessionID (= the real game_id since #845 PR A).
func TestPerGameSpans_CarryGameID(t *testing.T) {
	const gameID = "game_abc123def456"
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 10},
	}
	l, sr := recordingLoop(t, snap, []Decision{{Intervention: "grant_shield", Reason: "x"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: gameID, GameKind: "shadow"}, testTrigger())

	ended := sr.Ended()
	// Every per-game span family in this cycle must carry game.id == the session id.
	for _, name := range []string{SignalReceived, SpanDecision, SpanAction} {
		spans := spansByName(ended, name)
		if len(spans) != 1 {
			t.Fatalf("%s spans = %d, want 1", name, len(spans))
		}
		s := spans[0]
		if v, ok := attrValue(s, AttrGameID); !ok || v.AsString() != gameID {
			t.Errorf("%s: %s = %q (present=%v), want %q", name, AttrGameID, v.AsString(), ok, gameID)
		}
		if v, ok := attrValue(s, AttrSessionID); !ok || v.AsString() != gameID {
			t.Errorf("%s: %s = %q (present=%v), want %q", name, AttrSessionID, v.AsString(), ok, gameID)
		}
	}
}

// TestDisabledSpan_EmittedWithFlagAttribution: the kill switch (enabled=false)
// still emits a trace showing "agent off" with the flags in effect (#729 §4).
func TestDisabledSpan_EmittedWithFlagAttribution(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:    false,
		Mode:       "rules",
		Capability: flags.Capability{Model: "phi4-mini", PromptVariant: "conservative"},
		Policy:     flags.Policy{BatteryThreshold: 20, MaxInterventionsPerMinute: 2},
	}
	// Rules would return a decision, but the disabled path must short-circuit
	// before they run — the span must still be emitted.
	l, sr := recordingLoop(t, snap, []Decision{{Intervention: "play_audio_cue"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	ended := sr.Ended()
	roots := spansByName(ended, SignalReceived)
	disabled := spansByName(ended, SpanDisabled)
	if len(roots) != 1 || len(disabled) != 1 {
		t.Fatalf("spans = %d root + %d disabled, want 1/1 (no action span)", len(roots), len(disabled))
	}
	if len(spansByName(ended, SpanAction)) != 0 {
		t.Error("disabled path must not emit an action span")
	}
	d := disabled[0]
	if v, ok := attrValue(d, AttrEnabled); !ok || v.AsBool() {
		t.Error("agent.enabled must be false on the disabled span")
	}
	if v, ok := attrValue(d, AttrModel); !ok || v.AsString() != "phi4-mini" {
		t.Errorf("agent.model = %q, want phi4-mini (capability still recorded)", v.AsString())
	}
	if v, ok := attrValue(d, AttrPolicyBatteryThreshold); !ok || v.AsInt64() != 20 {
		t.Errorf("policy.battery_threshold = %d, want 20", v.AsInt64())
	}
	if d.Parent().SpanID() != roots[0].SpanContext().SpanID() {
		t.Error("disabled span must be a child of agent.signal_received")
	}
}

// TestDisabledSpan_Throttled: a disabled agent under heavy signal load emits at
// most one kill-switch trace per throttle interval, not one per export.
func TestDisabledSpan_Throttled(t *testing.T) {
	snap := flags.Snapshot{Enabled: false, Mode: "rules"}
	l, sr := recordingLoop(t, snap, nil)

	for i := 0; i < 20; i++ {
		l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())
	}
	if n := len(spansByName(sr.Ended(), SpanDisabled)); n != 1 {
		t.Errorf("disabled spans = %d, want 1 (throttled)", n)
	}
}

// TestDisabledSpan_LLMModeNoFallbackReason: when the kill switch is off
// (enabled=false) in llm mode, the disabled span reports inference.used=rules and
// inference.fallback_reason="" — NOT a fallback reason. The disabled path
// short-circuits before decide()/resolve_backend run, so no inference was
// attempted and there is nothing to fall back FROM (#741). This locks the #741
// attribution change: pre-#741 the static inferenceAttribution(mode) stamped
// no_backend_available here even though the agent was off; now the resolved
// LayerState (decide never ran) honestly carries an empty fallback reason.
func TestDisabledSpan_LLMModeNoFallbackReason(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:    false,
		Mode:       "llm",
		Capability: flags.Capability{Model: "claude", PromptVariant: "balanced"},
	}
	l, sr := recordingLoop(t, snap, []Decision{{Intervention: "play_audio_cue"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1"}, testTrigger())

	disabled := spansByName(sr.Ended(), SpanDisabled)
	if len(disabled) != 1 {
		t.Fatalf("disabled spans = %d, want 1", len(disabled))
	}
	d := disabled[0]
	if v, ok := attrValue(d, AttrMode); !ok || v.AsString() != "llm" {
		t.Errorf("agent.mode = %q, want llm", v.AsString())
	}
	if v, ok := attrValue(d, AttrInferenceUsed); !ok || v.AsString() != InferenceRules {
		t.Errorf("inference.used = %q, want %q (nothing decided; rules floor)", v.AsString(), InferenceRules)
	}
	if v, ok := attrValue(d, AttrInferenceFallback); !ok || v.AsString() != "" {
		t.Errorf("inference.fallback_reason = %q, want \"\" (agent off, no inference attempted)", v.AsString())
	}
	if v, ok := attrValue(d, AttrInferenceConfigured); !ok || v.AsString() != "claude" {
		t.Errorf("inference.configured = %q, want claude (capability still recorded)", v.AsString())
	}
}

// TestInferenceAttribution_LLMFallback: mode=llm records the fallback to rules
// with a reason; mode=rules records no fallback. Path-agnostic per #729 §5.
func TestInferenceAttribution_LLMFallback(t *testing.T) {
	tests := []struct {
		name         string
		mode         string
		wantUsed     string
		wantFallback string
	}{
		{name: "rules mode", mode: "rules", wantUsed: InferenceRules, wantFallback: ""},
		{name: "llm mode falls back", mode: "llm", wantUsed: InferenceRules, wantFallback: FallbackNoBackend},
		{name: "unknown mode runs rules", mode: "weird", wantUsed: InferenceRules, wantFallback: ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			snap := flags.Snapshot{
				Enabled:              true,
				Mode:                 tc.mode,
				Capability:           flags.Capability{Model: "phi4-mini"},
				InterventionsAllowed: []string{"play_audio_cue"},
				Policy:               flags.Policy{MaxInterventionsPerMinute: 10},
				// Gate-ALLOWED config (#847): the llm case asserts the unchanged
				// no_backend_available fallback, which only holds when the gate admits —
				// so this game kind ("") is eligible, no cadence floor, ample budget.
				LLMGate: flags.LLMGate{
					EligibleGameKinds:    []string{""},
					MinDecisionInterval:  0,
					MaxRequestsPerMinute: 100,
				},
			}
			l, sr := recordingLoop(t, snap, []Decision{{Intervention: "play_audio_cue"}})

			l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

			dec := spansByName(sr.Ended(), SpanDecision)[0]
			if v, ok := attrValue(dec, AttrInferenceConfigured); !ok || v.AsString() != "phi4-mini" {
				t.Errorf("inference.configured = %q, want phi4-mini", v.AsString())
			}
			if v, ok := attrValue(dec, AttrInferenceUsed); !ok || v.AsString() != tc.wantUsed {
				t.Errorf("inference.used = %q, want %q", v.AsString(), tc.wantUsed)
			}
			if v, ok := attrValue(dec, AttrInferenceFallback); !ok || v.AsString() != tc.wantFallback {
				t.Errorf("inference.fallback_reason = %q, want %q", v.AsString(), tc.wantFallback)
			}
		})
	}
}

// TestFitnessHook_CycleLevel verifies the #731 hook: LayerState.FitnessEvaluated
// is absent from the span when empty (the steady state until #731) and lifted as
// fitness.evaluated when populated.
func TestFitnessHook_CycleLevel(t *testing.T) {
	// Empty: attribute absent (no "evaluated nothing" placeholder at cycle scope).
	emptyAttrs := layerStateAttributes(&LayerState{Enabled: true, Mode: "rules"})
	for _, kv := range emptyAttrs {
		if string(kv.Key) == AttrFitnessEvaluated {
			t.Fatalf("fitness.evaluated must be absent when FitnessEvaluated is empty")
		}
	}

	// Populated (simulating #731): lifted as a sorted k=v slice.
	state := &LayerState{
		Enabled:          true,
		Mode:             "rules",
		FitnessEvaluated: map[string]float64{"session_duration": 95, "target_session_seconds": 60},
	}
	var got []string
	for _, kv := range layerStateAttributes(state) {
		if string(kv.Key) == AttrFitnessEvaluated {
			got = kv.Value.AsStringSlice()
		}
	}
	want := []string{"session_duration=95", "target_session_seconds=60"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("fitness.evaluated = %v, want %v", got, want)
	}
}
