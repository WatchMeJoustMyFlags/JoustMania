package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/llm"
)

// proposer_gate_main_test.go covers the main.go wiring of the #1270 proposer
// gates: the kill-switch Permit (enabled + experiments_enabled, read live) and the
// shared-budget admission installed only for a real text backend.

type fakeProposerGateFlags struct {
	agentEnabled bool
	// experiments is the live flag value; nil = flag absent / flagd down, so the
	// caller's default applies.
	experiments *bool
}

func (f fakeProposerGateFlags) AgentEnabled(context.Context) bool { return f.agentEnabled }
func (f fakeProposerGateFlags) ExperimentsEnabled(_ context.Context, def bool) bool {
	if f.experiments == nil {
		return def
	}
	return *f.experiments
}

func boolPtr(b bool) *bool { return &b }

// UC: UC-042 A5 BR-002
func TestProposerPermit(t *testing.T) {
	cases := []struct {
		name       string
		src        fakeProposerGateFlags
		envDefault bool
		wantOK     bool
		wantReason string
	}{
		{"both on", fakeProposerGateFlags{true, boolPtr(true)}, false, true, ""},
		{"kill switch off", fakeProposerGateFlags{false, boolPtr(true)}, false, false, experiment.SkipReasonAgentDisabled},
		{"experiments off", fakeProposerGateFlags{true, boolPtr(false)}, true, false, experiment.SkipReasonExperimentsDisabled},
		{"experiments flag absent, env default off", fakeProposerGateFlags{true, nil}, false, false, experiment.SkipReasonExperimentsDisabled},
		{"experiments flag absent, env default on", fakeProposerGateFlags{true, nil}, true, true, ""},
		{"both off reports the kill switch", fakeProposerGateFlags{false, boolPtr(false)}, false, false, experiment.SkipReasonAgentDisabled},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			ok, reason := proposerPermit(tc.src, tc.envDefault)(context.Background())
			if ok != tc.wantOK || reason != tc.wantReason {
				t.Fatalf("permit = (%v, %q), want (%v, %q)", ok, reason, tc.wantOK, tc.wantReason)
			}
		})
	}
}

// The permit is read at USE time: flipping the source after construction changes
// the next answer (no startup caching).
// UC: UC-042 A5 BR-002
func TestProposerPermit_ReadsLive(t *testing.T) {
	src := &liveGateFlags{enabled: true, experiments: true}
	permit := proposerPermit(src, false)
	if ok, _ := permit(context.Background()); !ok {
		t.Fatal("want permitted while both flags are on")
	}
	src.enabled = false
	if ok, reason := permit(context.Background()); ok || reason != experiment.SkipReasonAgentDisabled {
		t.Fatalf("permit = (%v, %q) after kill switch flip, want (false, agent_disabled)", ok, reason)
	}
}

type liveGateFlags struct{ enabled, experiments bool }

func (f *liveGateFlags) AgentEnabled(context.Context) bool { return f.enabled }
func (f *liveGateFlags) ExperimentsEnabled(context.Context, bool) bool {
	return f.experiments
}

type snapshotFlags struct{ snap flags.Snapshot }

func (f snapshotFlags) Evaluate(context.Context) flags.Snapshot { return f.snap }

func realOnlyGate(maxPerMinute int) snapshotFlags {
	return snapshotFlags{snap: flags.Snapshot{LLMGate: flags.LLMGate{
		EligibleGameKinds: []string{"real"}, MaxRequestsPerMinute: maxPerMinute,
	}}}
}

// With the stub backend no admission is installed, so a propose cycle never
// charges the shared LLM budget; with a real backend each model call takes a slot.
// UC: UC-042 A2 BR-001
func TestProposerAdmissionFor_StubNeverChargesBudget(t *testing.T) {
	budget := decision.NewLLMBudget()
	src := realOnlyGate(1)

	if adm := proposerAdmissionFor(nil, decision.NewLLMAdmission(src, budget, 0, "proposer", nil)); adm != nil {
		t.Fatalf("stub backend got admission %T, want untyped nil", adm)
	}

	// Drive a full stub propose cycle with the stub's (nil) admission.
	p := experiment.NewProposer(proposeBackendFor(nil), nil, nil, nil, nil)
	p.SetLLMAdmission(proposerAdmissionFor(nil, decision.NewLLMAdmission(src, budget, 0, "proposer", nil)))
	res := p.ProposeOnce(context.Background(), experiment.Narrative{}, experiment.ProposerConfig{
		Permit: func(context.Context) (bool, string) { return true, "" }, GameKind: "real",
	})
	if res.Outcome != experiment.ProposeNoBackend {
		t.Fatalf("stub outcome = %q, want no_backend", res.Outcome)
	}
	// The single slot is still free.
	if ok, reason := decision.NewLLMAdmission(src, budget, 0, "proposer", nil).Admit(context.Background(), "real"); !ok {
		t.Fatalf("budget slot was charged by a stub cycle (%q)", reason)
	}
}

// UC: UC-042 A2 BR-001
func TestProposerAdmissionFor_RealBackendChargesSharedBudget(t *testing.T) {
	budget := decision.NewLLMBudget()
	src := realOnlyGate(1)
	delegate := func(context.Context, llm.Prompt) (string, error) { return "not json", nil }

	p := experiment.NewProposer(proposeBackendFor(delegate), nil, nil, nil, nil)
	p.SetLLMAdmission(proposerAdmissionFor(delegate, decision.NewLLMAdmission(src, budget, 0, "proposer", nil)))
	cfg := experiment.ProposerConfig{
		Permit: func(context.Context) (bool, string) { return true, "" }, GameKind: "real",
	}
	if got := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg).Outcome; got != experiment.ProposeUndecodable {
		t.Fatalf("first cycle = %q, want undecodable (the model was called)", got)
	}
	// The one-per-minute budget is now spent: the next cycle is gated.
	res := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
	if res.Outcome != experiment.ProposeLLMGated || res.Reason != decision.FallbackBudgetExhausted {
		t.Fatalf("second cycle = (%q, %q), want (llm_gated, llm_budget_exhausted)", res.Outcome, res.Reason)
	}
}
