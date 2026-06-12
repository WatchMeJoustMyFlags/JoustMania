package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// llm_decide_test.go drives the #739 LLM decision path end-to-end through the loop
// with a FAKE backend, asserting every acceptance criterion: the llm path is taken
// (not rules) on a valid response; objectives reach the prompt; an unparseable /
// invalid / out-of-vocab / errored response dispatches NO arbitrary action and
// falls back with the recorded reason; the SAME permission gate + rate limit apply
// to llm decisions; and the inference span carries used=<tier> + the reasoning.

// inferBackend is the #739 inference fake: always available, returns a canned raw
// response (or a canned error) for every Infer call, and records the last prompt
// so a test can assert objective-aware prompting. Distinct from fakeBackend (the
// #741 resolver fake) so the two concerns stay separate.
type inferBackend struct {
	name             string
	response         string // raw text returned from Infer (the model's reply)
	inferErr         error  // when non-nil, Infer returns this instead of response
	lastPromptSystem string
	lastPromptUser   string
	calls            int
}

func (b *inferBackend) Name() string                   { return b.name }
func (b *inferBackend) Available(context.Context) bool { return true }
func (b *inferBackend) Infer(_ context.Context, p llm.Prompt) (string, error) {
	b.calls++
	b.lastPromptSystem = p.System
	b.lastPromptUser = p.User
	if b.inferErr != nil {
		return "", b.inferErr
	}
	return b.response, nil
}

// resolverWith builds a resolver whose single-tier chain is the given backend, so
// resolve(model) resolves to it (model "phi4-mini" selects the phi tier name). The
// cache is primed (Available()=true). It is the seam that injects a #739 inference
// fake into the live loop.
func resolverWith(b Backend) *Resolver {
	r := NewResolver([]Backend{b}, 0)
	r.Refresh(context.Background())
	return r
}

// llmDecideLoop wires a loop for the #739 path: recording tracer, an always-admit
// llm gate, the given resolver (single fake tier), and a rules engine whose output
// is the RULES fallback (so a test can tell an llm decision from a rules one by the
// decision.reason). Returns the loop, span recorder, and the action sink.
func llmDecideLoop(t *testing.T, snap flags.Snapshot, resolver *Resolver, rulesOut []Decision) (*Loop, *tracetest.SpanRecorder, *fakeSink) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	sink := &fakeSink{}
	l := NewLoop(&settableFlags{snap: snap}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = fakeRules{out: rulesOut}
	l.Actions = sink
	l.llmGated = newLLMGatedCounter(mp)
	l.SetLLMBudget(NewLLMBudget())
	l.SetResolver(resolver)
	return l, sr, sink
}

// llmDecideSnapshot is an llm-mode snapshot whose #847 gate always admits and whose
// model (phi4-mini) selects the single-tier fake. The allow-list contains the
// interventions the valid-response tests choose, but NOT the out-of-allow-list one.
func llmDecideSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		Objectives:           map[string]float64{"endurance": 0.7, "chaos": 0.3},
		Capability:           flags.Capability{Model: "phi4-mini", PromptVariant: "balanced"},
		InterventionsAllowed: []string{"noop", "grant_shield", "play_audio_cue"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		LLMGate: flags.LLMGate{
			EligibleGameKinds:    []string{"real"},
			MinDecisionInterval:  0,
			MaxRequestsPerMinute: 100,
		},
	}
}

const validShieldResponse = `{"intervention":"grant_shield","target_serial":"","value":"","reason":"llm chose this","objective_served":"endurance"}`

// --- Acceptance: the llm path is TAKEN (not rules) on a valid response ---

func TestLLMDecide_ValidResponseTakesLLMPath(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	// The rules engine would return a DISTINCT decision; if the llm path is taken,
	// the dispatched decision must be the LLM's, not the rules engine's.
	l, sr, sink := llmDecideLoop(t, llmDecideSnapshot(), resolverWith(be), []Decision{{Intervention: "play_audio_cue", Reason: "from rules"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1 (llm path taken)", be.calls)
	}
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "llm chose this" {
		t.Errorf("decision.reason = %q, want the LLM's reason (not the rules fallback)", v.AsString())
	}
	if v, ok := attrValue(decs[0], AttrDecisionAction); !ok || v.AsString() != "grant_shield" {
		t.Errorf("decision.action = %q, want grant_shield (the LLM choice)", v.AsString())
	}
	if v, ok := attrValue(decs[0], AttrInferenceUsed); !ok || v.AsString() != "phi4-mini" {
		t.Errorf("inference.used = %q, want phi4-mini (the tier that decided)", v.AsString())
	}
	if v, ok := attrValue(decs[0], AttrInferenceFallback); !ok || v.AsString() != "" {
		t.Errorf("inference.fallback_reason = %q, want empty (llm decided cleanly)", v.AsString())
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (llm decision dispatched)", sink.calls.Load())
	}
	// The inference span exists and carries the model's reasoning.
	inf := spansByName(sr.Ended(), SpanLLMInfer)
	if len(inf) != 1 {
		t.Fatalf("agent.llm.infer spans = %d, want 1", len(inf))
	}
	if v, ok := attrValue(inf[0], AttrDecisionReason); !ok || v.AsString() != "llm chose this" {
		t.Errorf("infer span decision.reason = %q, want the model reasoning", v.AsString())
	}
	if v, ok := attrValue(inf[0], "gen_ai.request.model"); !ok || v.AsString() != "phi4-mini" {
		t.Errorf("infer span gen_ai.request.model = %q, want phi4-mini", v.AsString())
	}
}

// --- Acceptance: objectives reach the prompt ---

func TestLLMDecide_ObjectivesShapePrompt(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.Objectives = map[string]float64{"endurance": 0.8, "accelerate": 0.2}
	l, _, _ := llmDecideLoop(t, snap, resolverWith(be), nil)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}
	// The System prompt must encode the objective weights (objective-aware prompting).
	for _, want := range []string{"endurance=0.8", "accelerate=0.2"} {
		if !strings.Contains(be.lastPromptSystem, want) {
			t.Errorf("prompt System missing %q; the objectives must shape the prompt\n--- system ---\n%s", want, be.lastPromptSystem)
		}
	}
	// And the allow-list, also objective context for the model.
	if !strings.Contains(be.lastPromptSystem, "grant_shield") {
		t.Error("prompt System must list the allowed interventions")
	}
}

// --- Acceptance: a noop response dispatches nothing but is NOT a rules fallback ---

func TestLLMDecide_NoopDispatchesNothing(t *testing.T) {
	be := &inferBackend{name: "phi4-mini",
		response: `{"intervention":"noop","reason":"nothing actionable","objective_served":"endurance"}`}
	// Rules WOULD return a decision; a clean noop must NOT trigger the rules fallback.
	l, sr, sink := llmDecideLoop(t, llmDecideSnapshot(), resolverWith(be), []Decision{{Intervention: "grant_shield", Reason: "from rules"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 1 {
		t.Fatalf("backend Infer calls = %d, want 1", be.calls)
	}
	if n := len(spansByName(sr.Ended(), SpanDecision)); n != 0 {
		t.Errorf("agent.decision spans = %d, want 0 (noop dispatches nothing, and rules did NOT run)", n)
	}
	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (noop)", sink.calls.Load())
	}
	// The inference span still records the model's reasoning.
	if n := len(spansByName(sr.Ended(), SpanLLMInfer)); n != 1 {
		t.Errorf("agent.llm.infer spans = %d, want 1", n)
	}
}

// --- Acceptance: unparseable / invalid / errored response falls back to rules,
//     dispatching NO arbitrary action, with FallbackUnparseable recorded ---

func TestLLMDecide_BadResponseFallsBackToRules(t *testing.T) {
	cases := []struct {
		name     string
		response string
		inferErr error
	}{
		{"empty", "", nil},
		{"not json", "I will not comply.", nil},
		{"malformed json", `{"intervention":}`, nil},
		{"missing reason", `{"intervention":"grant_shield","objective_served":"endurance"}`, nil},
		{"out-of-vocab objective", `{"intervention":"grant_shield","reason":"r","objective_served":"win"}`, nil},
		{"infer error", "", errors.New("backend exploded")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			be := &inferBackend{name: "phi4-mini", response: tc.response, inferErr: tc.inferErr}
			// The RULES engine returns a recognizable decision; the cycle must dispatch
			// IT (the safe deterministic fallback), never anything from the bad response.
			l, sr, sink := llmDecideLoop(t, llmDecideSnapshot(), resolverWith(be), []Decision{{Intervention: "grant_shield", Reason: "rules fallback"}})

			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

			if be.calls != 1 {
				t.Fatalf("backend Infer calls = %d, want 1", be.calls)
			}
			decs := spansByName(sr.Ended(), SpanDecision)
			if len(decs) != 1 {
				t.Fatalf("agent.decision spans = %d, want 1 (the rules fallback)", len(decs))
			}
			// The dispatched decision is the RULES one — no arbitrary action from the
			// untrusted response leaked through.
			if v, ok := attrValue(decs[0], AttrDecisionReason); !ok || v.AsString() != "rules fallback" {
				t.Errorf("decision.reason = %q, want the rules fallback (untrusted output must NOT dispatch)", v.AsString())
			}
			// inference.used stays the called tier (it answered, just unusably) and the
			// fallback reason is recorded on the span.
			if v, ok := attrValue(decs[0], AttrInferenceUsed); !ok || v.AsString() != "phi4-mini" {
				t.Errorf("inference.used = %q, want phi4-mini (the tier that was called)", v.AsString())
			}
			if v, ok := attrValue(decs[0], AttrInferenceFallback); !ok || v.AsString() != FallbackUnparseable {
				t.Errorf("inference.fallback_reason = %q, want %q", v.AsString(), FallbackUnparseable)
			}
			if sink.calls.Load() != 1 {
				t.Errorf("action sink calls = %d, want 1 (the rules fallback dispatched)", sink.calls.Load())
			}
		})
	}
}

// --- Acceptance: the permission gate applies IDENTICALLY to an llm decision ---

func TestLLMDecide_OutOfAllowListBlocked(t *testing.T) {
	// The model chooses an intervention NOT in interventions.allowed; the SAME
	// allow-list gate the rules path uses must block it (decision.blocked=true) and
	// it must NOT reach the action sink. No llm-specific bypass.
	be := &inferBackend{name: "phi4-mini",
		response: `{"intervention":"eliminate_player","target_serial":"AAAA","reason":"chaos","objective_served":"chaos"}`}
	l, sr, sink := llmDecideLoop(t, llmDecideSnapshot(), resolverWith(be), nil)

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 1 {
		t.Fatalf("agent.decision spans = %d, want 1 (a blocked decision is still audited)", len(decs))
	}
	if v, ok := attrValue(decs[0], AttrDecisionBlocked); !ok || !v.AsBool() {
		t.Error("decision.blocked must be true for an out-of-allow-list LLM action")
	}
	if v, ok := attrValue(decs[0], AttrDecisionBlockReason); !ok || v.AsString() != string(ReasonNotAllowed) {
		t.Errorf("block reason = %q, want %q", v.AsString(), ReasonNotAllowed)
	}
	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (blocked action never dispatched)", sink.calls.Load())
	}
}

// --- Acceptance: the weighted rate limit applies IDENTICALLY to llm decisions ---

func TestLLMDecide_RateLimitApplies(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.Policy.MaxInterventionsPerMinute = 1 // one weighted slot per minute
	clock := time.Unix(2000, 0)
	l, sr, sink := llmDecideLoop(t, snap, resolverWith(be), nil)
	l.now = func() time.Time { return clock }
	ctx := gamecontext.GameContext{SessionID: "s1", GameKind: "real"}

	l.OnEvaluate(context.Background(), ctx, testTrigger()) // dispatches (slot 1)
	clock = clock.Add(time.Second)
	l.OnEvaluate(context.Background(), ctx, testTrigger()) // rate-limited

	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) != 2 {
		t.Fatalf("agent.decision spans = %d, want 2", len(decs))
	}
	// The second llm decision is blocked by the SAME weighted rate limiter the rules
	// path uses — not silently dropped, recorded with the rate_limit reason.
	if v, ok := attrValue(decs[1], AttrDecisionBlocked); !ok || !v.AsBool() {
		t.Error("second llm decision must be rate-limited (blocked)")
	}
	if v, ok := attrValue(decs[1], AttrDecisionBlockReason); !ok || v.AsString() != string(ReasonRateLimit) {
		t.Errorf("second block reason = %q, want %q", v.AsString(), ReasonRateLimit)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (only the first llm decision dispatched)", sink.calls.Load())
	}
}

// --- Acceptance: a gate-DENIED llm cycle never calls the backend (gate first) ---

func TestLLMDecide_GateDeniedNeverInfers(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	snap := llmDecideSnapshot()
	snap.LLMGate.EligibleGameKinds = []string{"real"} // a shadow game is gated
	l, sr, _ := llmDecideLoop(t, snap, resolverWith(be), []Decision{{Intervention: "grant_shield", Reason: "rules"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "shadow"}, testTrigger())

	if be.calls != 0 {
		t.Errorf("backend Infer calls = %d, want 0 (gate denied -> no inference)", be.calls)
	}
	_, used, fallback := inferenceTriple(t, sr)
	if used != InferenceRules {
		t.Errorf("inference.used = %q, want rules (gate denied)", used)
	}
	if fallback != FallbackNotEligible {
		t.Errorf("inference.fallback_reason = %q, want %q (the gate reason)", fallback, FallbackNotEligible)
	}
}

// --- The dev default: no resolver / unreachable chain keeps the rules behavior ---

func TestLLMDecide_UnreachableChainKeepsRules(t *testing.T) {
	be := &inferBackend{name: "phi4-mini", response: validShieldResponse}
	r := NewResolver([]Backend{be}, 0) // NOT refreshed -> cache empty -> tier unreachable
	l, sr, sink := llmDecideLoop(t, llmDecideSnapshot(), r, []Decision{{Intervention: "grant_shield", Reason: "rules"}})

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s1", GameKind: "real"}, testTrigger())

	if be.calls != 0 {
		t.Errorf("backend Infer calls = %d, want 0 (chain unreachable -> rules, no call)", be.calls)
	}
	_, used, fallback := inferenceTriple(t, sr)
	if used != InferenceRules {
		t.Errorf("inference.used = %q, want rules", used)
	}
	if fallback != FallbackNoBackend {
		t.Errorf("inference.fallback_reason = %q, want %q", fallback, FallbackNoBackend)
	}
	if sink.calls.Load() != 1 {
		t.Errorf("action sink calls = %d, want 1 (rules decision dispatched)", sink.calls.Load())
	}
}
