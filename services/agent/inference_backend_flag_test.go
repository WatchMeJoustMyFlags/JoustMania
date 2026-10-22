package main

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// inference_backend_flag_test.go covers inference.backend (#1810, C-035, FR-150..152):
// the flag decides, the stub applies while flagd cannot resolve it (the
// AGENT_INFERENCE_BACKEND env is gone, #1907); the env ceilings (key, base URL) still
// decide whether claude is usable; the choice is read per use.

type lockedBuf struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuf) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}
func (l *lockedBuf) String() string { l.mu.Lock(); defer l.mu.Unlock(); return l.b.String() }

func backendsOver(t *testing.T, ev *tuningEvaluator, logger *slog.Logger) *textBackends {
	t.Helper()
	return newTextBackends(logger, nil, flags.New(ev, quietLogger()))
}

// UC: UC-042 Pre-2
func TestInferenceBackendFlag_Resolution(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "")
	ctx := context.Background()

	cases := []struct {
		name       string
		flag       string // "" = flag not set (resolution error)
		down       bool
		wantClaude bool
	}{
		{"UC-042/Pre-2 flag claude is claude", "claude", false, true},
		{"UC-042/Pre-2 flag stub is stub", "stub", false, false},
		{"UC-042/Pre-2 flagd down is stub", "claude", true, false},
		{"UC-042/Pre-2 flag missing is stub", "", false, false},
		{"UC-042/Pre-2 unknown flag value is stub", "gpt-9000", false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ev := newTuningEvaluator()
			if tc.flag != "" {
				ev.setString("inference.backend", tc.flag)
			}
			ev.setDown(tc.down)
			client, name := backendsOver(t, ev, quietLogger()).Select(ctx)
			if (client != nil) != tc.wantClaude || (name == "claude") != tc.wantClaude {
				t.Fatalf("Select = (%v, %q), want claude=%v", client != nil, name, tc.wantClaude)
			}
		})
	}
}

// The env ceilings stay: claude by flag without a key, or with a rejected base URL,
// is the stub, and the key is sent nowhere.
// UC: UC-042 Pre-2
func TestInferenceBackendFlag_CeilingsStillApply(t *testing.T) {
	ev := newTuningEvaluator()
	ev.setString("inference.backend", "claude")

	t.Setenv("ANTHROPIC_API_KEY", "")
	if client, name := backendsOver(t, ev, quietLogger()).Select(context.Background()); client != nil || name != "stub" {
		t.Errorf("claude flag without a key = (%v, %q), want the stub", client != nil, name)
	}

	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "http://proxy.example.com")
	if client, name := backendsOver(t, ev, quietLogger()).Select(context.Background()); client != nil || name != "stub" {
		t.Errorf("claude flag with a rejected base URL = (%v, %q), want the stub", client != nil, name)
	}
}

// A flip is seen on the next use, the client is built once, and the WARN for a
// degraded claude request is logged once per change.
// UC: UC-042 Pre-2
func TestInferenceBackendFlag_RuntimeFlipAndWarnOncePerChange(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "")
	logs := &lockedBuf{}
	ev := newTuningEvaluator()
	tb := backendsOver(t, ev, slog.New(slog.NewTextHandler(logs, nil)))
	ctx := context.Background()

	ev.setString("inference.backend", "claude")
	for i := 0; i < 5; i++ { // claude requested, no key: the stub every time
		if c, _ := tb.Select(ctx); c != nil {
			t.Fatal("claude without a key must be the stub")
		}
	}
	if n := strings.Count(logs.String(), "ANTHROPIC_API_KEY is empty"); n != 1 {
		t.Fatalf("degraded-claude WARN logged %d times over 5 uses, want once per change:\n%s", n, logs.String())
	}
	ev.setString("inference.backend", "stub")
	tb.Select(ctx)
	ev.setString("inference.backend", "claude")
	tb.Select(ctx)
	if n := strings.Count(logs.String(), "ANTHROPIC_API_KEY is empty"); n != 2 {
		t.Fatalf("WARN count after stub->claude again = %d, want 2", n)
	}

	// With a key the same flip reaches the next call and reuses ONE client.
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	tb = backendsOver(t, ev, quietLogger())
	ev.setString("inference.backend", "stub")
	if c, _ := tb.Select(ctx); c != nil {
		t.Fatal("stub flag must give the stub")
	}
	ev.setString("inference.backend", "claude")
	first, _ := tb.Select(ctx)
	second, _ := tb.Select(ctx)
	if first == nil || first != second {
		t.Fatal("claude flag must return the same client built once")
	}
	ev.setString("inference.backend", "stub")
	if c, _ := tb.Select(ctx); c != nil {
		t.Fatal("flip back to stub must reach the next use")
	}
}

// RetroBackend returns an untyped nil for the stub (a typed nil would look wired).
// UC: UC-042 Pre-2
func TestInferenceBackendFlag_RetroSourceNilForStub(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "")
	ev := newTuningEvaluator()
	tb := backendsOver(t, ev, quietLogger())
	ev.setString("inference.backend", "stub")
	if b := tb.RetroBackend(context.Background()); b != nil {
		t.Fatalf("stub retro backend = %T, want an untyped nil", b)
	}
	ev.setString("inference.backend", "claude")
	var _ decision.TextBackend = tb.claude
	if b := tb.RetroBackend(context.Background()); b == nil {
		t.Fatal("claude retro backend = nil")
	}
}

// The proposer resolves the backend once per cycle; a stub cycle after a claude->stub
// flip is never charged to the shared budget (the admission is always installed).
// UC: UC-042 A7 BR-001
// NFR: NFR-011
func TestInferenceBackendFlag_ProposerStubAfterFlipNeverCharges(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "")
	ev := newTuningEvaluator()
	tb := backendsOver(t, ev, quietLogger())
	budget := decision.NewLLMBudget()
	gate := realOnlyGate(1)

	p := newWiredProposer(tb.ProposerBackend, nil, nil, decision.NewLLMAdmission(gate, budget, 0, "proposer", nil), nil)
	cfg := experiment.ProposerConfig{
		Permit: func(context.Context) (bool, string) { return true, "" }, GameKind: "real",
	}

	ev.setString("inference.backend", "stub")
	if got := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg).Outcome; got != experiment.ProposeNoBackend {
		t.Fatalf("stub outcome = %q, want no_backend", got)
	}
	if ok, reason := decision.NewLLMAdmission(gate, budget, 0, "proposer", nil).Admit(context.Background(), "real"); !ok {
		t.Fatalf("a stub proposer cycle charged the budget (%q)", reason)
	}
}

// Claude chosen by the flag takes the shared budget slot (the cycle is admitted
// before the model call, so the second cycle in the minute is gated).
// UC: UC-042 A7 BR-001
// NFR: NFR-011
func TestInferenceBackendFlag_ProposerClaudeChargesSharedBudget(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	// A loopback port nothing listens on: the call fails fast after admission.
	t.Setenv("AGENT_INFERENCE_BASE_URL", "http://127.0.0.1:1")
	ev := newTuningEvaluator()
	ev.setString("inference.backend", "claude")
	tb := backendsOver(t, ev, quietLogger())
	budget := decision.NewLLMBudget()
	gate := realOnlyGate(1)

	p := newWiredProposer(tb.ProposerBackend, nil, nil, decision.NewLLMAdmission(gate, budget, 0, "proposer", nil), nil)
	cfg := experiment.ProposerConfig{
		Permit: func(context.Context) (bool, string) { return true, "" }, GameKind: "real",
	}
	first := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
	if first.Outcome == experiment.ProposeLLMGated || first.Outcome == experiment.ProposeDisabled {
		t.Fatalf("first claude cycle = %q, want it to reach the backend", first.Outcome)
	}
	second := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
	if second.Outcome != experiment.ProposeLLMGated {
		t.Fatalf("second claude cycle = %q, want llm_gated (budget charged by the first)", second.Outcome)
	}
}

// The M7-4 proposer gate (experiment_proposer_enabled AND env, #1818) is checked
// before the backend is resolved: claude by flag with the gate closed makes no model
// call and spends no budget.
// UC: UC-042 A5 BR-002
// NFR: NFR-011
func TestInferenceBackendFlag_ClaudeWithProposerGateClosedNoProposalNoSpend(t *testing.T) {
	t.Setenv("ANTHROPIC_API_KEY", "sk-test")
	t.Setenv("AGENT_INFERENCE_BASE_URL", "http://127.0.0.1:1")
	ev := newTuningEvaluator()
	ev.setString("inference.backend", "claude")
	tb := backendsOver(t, ev, quietLogger())
	resolved := 0
	budget := decision.NewLLMBudget()
	gate := realOnlyGate(1)

	p := newWiredProposer(func(ctx context.Context) (experiment.Backend, bool) {
		resolved++
		return tb.ProposerBackend(ctx)
	}, nil, nil, decision.NewLLMAdmission(gate, budget, 0, "proposer", nil), nil)
	src := &liveGateFlags{enabled: true, experiments: true, proposer: false}
	cfg := experiment.ProposerConfig{Permit: proposerPermit(src, false, true), GameKind: "real"}
	res := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
	if res.Outcome != experiment.ProposeDisabled || res.Reason != experiment.SkipReasonProposerDisabled {
		t.Fatalf("outcome = (%q, %q), want (disabled, proposer_disabled)", res.Outcome, res.Reason)
	}
	if resolved != 0 {
		t.Fatalf("backend resolved %d times behind a closed gate, want 0", resolved)
	}
	if ok, reason := decision.NewLLMAdmission(gate, budget, 0, "proposer", nil).Admit(context.Background(), "real"); !ok {
		t.Fatalf("a closed-gate cycle charged the budget (%q)", reason)
	}
}
