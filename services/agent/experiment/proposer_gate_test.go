package experiment

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/joustmania/agent/llm"
)

// proposer_gate_test.go covers the #1270 gates on the post-game proposer: the
// `enabled` kill switch and `experiments_enabled` (ProposerConfig.Permit), and the
// shared LLM call gate (SetLLMAdmission). "Agent off" must be total: no model call,
// no budget slot, no game.json write.

// allowPropose is the always-on Permit for tests that exercise the propose path
// itself rather than the kill switch.
func allowPropose(context.Context) (bool, string) { return true, "" }

func denyPropose(reason string) func(context.Context) (bool, string) {
	return func(context.Context) (bool, string) { return false, reason }
}

const validReply = `{"flag":"death_grace_period_seconds","value":0.75,"rationale":"longer grace"}`

// fakeAdmission is a scripted experiment.LLMAdmission recording its calls.
type fakeAdmission struct {
	mu     sync.Mutex
	ok     bool
	reason string
	kinds  []string
}

func (a *fakeAdmission) Admit(_ context.Context, gameKind string) (bool, string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.kinds = append(a.kinds, gameKind)
	return a.ok, a.reason
}

func (a *fakeAdmission) calls() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	return len(a.kinds)
}

func TestProposeOnce_KillSwitchOrExperimentsOffDoesNothing(t *testing.T) {
	cases := []struct {
		name   string
		permit func(context.Context) (bool, string)
		reason string
	}{
		{"agent enabled off", denyPropose(SkipReasonAgentDisabled), SkipReasonAgentDisabled},
		{"experiments_enabled off", denyPropose(SkipReasonExperimentsDisabled), SkipReasonExperimentsDisabled},
		{"nil permit fails closed", nil, SkipReasonNoPermit},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			backend := &fakeProposerBackend{response: validReply}
			p, path, rec, _ := proposerWithRecorder(t, backend)
			adm := &fakeAdmission{ok: true}
			p.SetLLMAdmission(adm)

			res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{
				Engine: "claude", MinInterval: time.Minute, Permit: tc.permit, GameKind: "real",
			})

			if res.Outcome != ProposeDisabled || res.Reason != tc.reason {
				t.Fatalf("result = (%q, %q), want (%q, %q)", res.Outcome, res.Reason, ProposeDisabled, tc.reason)
			}
			if backend.calls != 0 {
				t.Fatalf("Infer calls = %d, want 0 (agent off must make no model call)", backend.calls)
			}
			if adm.calls() != 0 {
				t.Fatalf("LLM admissions = %d, want 0 (agent off must not charge the budget)", adm.calls())
			}
			if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
				t.Fatal("agent off must not write game.json")
			}
			if spanExists(rec, SpanProposeAttempt) || spanExists(rec, SpanProposed) {
				t.Fatal("agent off must emit no propose / gate span")
			}
		})
	}
}

// A disabled cycle must not consume the cadence window: the first cycle after the
// agent is switched back on proposes immediately.
func TestProposeOnce_DisabledCycleDoesNotAdvanceCadence(t *testing.T) {
	backend := &fakeProposerBackend{response: validReply}
	p, _, _, _ := proposerWithRecorder(t, backend)
	cfg := ProposerConfig{Engine: "stub", MinInterval: time.Hour, Permit: denyPropose(SkipReasonAgentDisabled)}

	if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeDisabled {
		t.Fatalf("outcome = %q, want disabled", got)
	}
	cfg.Permit = allowPropose
	if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
		t.Fatalf("outcome after re-enable = %q, want proposed (disabled cycle must not start the cadence window)", got)
	}
}

// The kill switch is re-read right before the write: a flip to off while the model
// call is in flight still prevents the game.json write, and the reason lands on the
// attempt span.
func TestProposeOnce_SwitchedOffDuringInferenceDoesNotWrite(t *testing.T) {
	var on atomic.Bool
	on.Store(true)
	backend := &switchingBackend{response: validReply, onInfer: func() { on.Store(false) }}
	p, path, rec, _ := proposerWithRecorder(t, backend)

	res := p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{
		Engine: "claude",
		Permit: func(context.Context) (bool, string) {
			if on.Load() {
				return true, ""
			}
			return false, SkipReasonAgentDisabled
		},
	})

	if res.Outcome != ProposeDisabled || res.Reason != SkipReasonAgentDisabled {
		t.Fatalf("result = (%q, %q), want (disabled, agent_disabled)", res.Outcome, res.Reason)
	}
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatal("a kill switch flipped during inference must prevent the game.json write")
	}
	if spanExists(rec, SpanProposed) {
		t.Fatal("Gate.Review must not run after the kill switch flipped")
	}
	span := findSpan(t, rec, SpanProposeAttempt)
	assertSpanAttr(t, span, AttrOutcome, string(ProposeDisabled))
	assertSpanAttr(t, span, AttrReason, SkipReasonAgentDisabled)
}

// switchingBackend returns a canned reply and runs onInfer during the call.
type switchingBackend struct {
	response string
	onInfer  func()
}

func (b *switchingBackend) Infer(context.Context, llm.Prompt) (string, error) {
	b.onInfer()
	return b.response, nil
}

// A refused LLM admission (budget exhausted / not eligible) skips the model call,
// writes nothing, reports the gate reason, and rolls the cadence window back.
func TestProposeOnce_LLMAdmissionRefusedSkipsCall(t *testing.T) {
	backend := &fakeProposerBackend{response: validReply}
	p, path, rec, _ := proposerWithRecorder(t, backend)
	adm := &fakeAdmission{ok: false, reason: "llm_budget_exhausted"}
	p.SetLLMAdmission(adm)
	cfg := ProposerConfig{Engine: "claude", MinInterval: time.Hour, Permit: allowPropose, GameKind: "real"}

	res := p.ProposeOnce(context.Background(), Narrative{}, cfg)
	if res.Outcome != ProposeLLMGated || res.Reason != "llm_budget_exhausted" {
		t.Fatalf("result = (%q, %q), want (llm_gated, llm_budget_exhausted)", res.Outcome, res.Reason)
	}
	if backend.calls != 0 {
		t.Fatalf("Infer calls = %d, want 0", backend.calls)
	}
	if _, ok := experimentVariantValue(t, path, "death_grace_period_seconds"); ok {
		t.Fatal("a gated cycle must not write game.json")
	}
	if spanExists(rec, SpanProposeAttempt) {
		t.Fatal("a gated cycle emits no attempt span (like a budget-gated retro)")
	}
	if adm.kinds[0] != "real" {
		t.Fatalf("admission saw game kind %q, want real", adm.kinds[0])
	}

	// Budget frees up: the very next cycle proposes, because the refused cycle did
	// not consume the hour-long cadence window.
	adm.ok, adm.reason = true, ""
	if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
		t.Fatalf("outcome = %q, want proposed (cadence must have been rolled back)", got)
	}
	if backend.calls != 1 || adm.calls() != 2 {
		t.Fatalf("Infer calls = %d, admissions = %d, want 1 and 2", backend.calls, adm.calls())
	}
}

// A cadence-gated cycle never reaches the LLM admission, so it spends no budget.
func TestProposeOnce_CadenceGatedCycleDoesNotChargeBudget(t *testing.T) {
	backend := &fakeProposerBackend{response: validReply}
	p, _, _, clk := proposerWithRecorder(t, backend)
	adm := &fakeAdmission{ok: true}
	p.SetLLMAdmission(adm)
	cfg := ProposerConfig{Engine: "claude", MinInterval: time.Minute, Permit: allowPropose, GameKind: "real"}

	if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeProposed {
		t.Fatalf("cycle 1 = %q, want proposed", got)
	}
	clk.advance(10 * time.Second)
	if got := p.ProposeOnce(context.Background(), Narrative{}, cfg).Outcome; got != ProposeGated {
		t.Fatalf("cycle 2 = %q, want gated", got)
	}
	if adm.calls() != 1 {
		t.Fatalf("admissions = %d, want 1 (cadence-gated cycle must not charge)", adm.calls())
	}
}

// Concurrent game ends (one shared Proposer, as in main.go) stay race-clean under
// the gates; with no cadence floor every admitted cycle proposes.
func TestProposeOnce_ConcurrentGatedCyclesRaceClean(t *testing.T) {
	p, _, _, _ := proposerWithRecorder(t, &lockedBackend{response: validReply})
	adm := &fakeAdmission{ok: true}
	p.SetLLMAdmission(adm)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			permit := allowPropose
			if i%2 == 0 {
				permit = denyPropose(SkipReasonAgentDisabled)
			}
			p.ProposeOnce(context.Background(), Narrative{}, ProposerConfig{Engine: "claude", Permit: permit, GameKind: "real"})
		}(i)
	}
	wg.Wait()
	if adm.calls() != 4 {
		t.Fatalf("admissions = %d, want 4 (only the permitted half)", adm.calls())
	}
}

// lockedBackend is a concurrency-safe canned backend.
type lockedBackend struct{ response string }

func (b *lockedBackend) Infer(context.Context, llm.Prompt) (string, error) { return b.response, nil }
