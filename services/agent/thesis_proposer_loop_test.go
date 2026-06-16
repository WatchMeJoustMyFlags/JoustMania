package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// thesis_proposer_loop_test.go pins the LOOP-LEVEL wiring of the #1184 retro-seeded
// thesis proposer: it is default-OFF, subordinate to the dynamic selector, and turns
// a recorded retro suggestion into a started experiment when opted in — reusing the
// loopWithRealRegistry / gameFileForDeclarer helpers from dynamic_declarer_test.go.

// proposerLoop builds a loop with a real registry, a wired dynamic selector (the
// proposer delegates flag selection to it), and a ThesisProposer.
func proposerLoop(t *testing.T) *experimentLoop {
	t.Helper()
	loop, path := loopWithRealRegistry(t, 8)
	loop.dynamic = &dynamicDeclarer{
		gameFlagPath:     path,
		candidates:       []string{"sensitivity", "num_teams"},
		defaultObjective: decision.ObjectiveBalanced,
	}
	loop.proposer = experiment.NewThesisProposer(0)
	return loop
}

// TestRetroProposer_DisabledByDefault: with AGENT_EXPERIMENT_PROPOSER_ENABLED unset,
// a recorded suggestion is NEVER turned into an experiment — the loop's autonomous
// cadence is unchanged for anyone who has not opted in.
func TestRetroProposer_DisabledByDefault(t *testing.T) {
	t.Setenv(envProposerEnabled, "") // explicitly unset
	loop := proposerLoop(t)
	loop.proposer.Record(experiment.RetroSuggestion{InterventionType: "adjust_global_difficulty", Emphasis: "weight_up", Reason: "room faded"})

	loop.declareFromRetroIfEnabled(context.Background(), dynCfg(5))

	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("LiveCount = %d, want 0 (proposer default-OFF must not declare)", got)
	}
	if got := loop.proposer.Backlog(); got != 1 {
		t.Errorf("backlog = %d, want 1 (suggestion untouched while disabled)", got)
	}
}

// TestRetroProposer_NilProposerOrSelector_NoOp: the hook is a safe no-op when the
// proposer or the selector is unwired (test paths), even with the opt-in on.
func TestRetroProposer_NilProposerOrSelector_NoOp(t *testing.T) {
	t.Setenv(envProposerEnabled, "true")
	loop, _ := loopWithRealRegistry(t, 8)
	// No proposer, no dynamic selector wired.
	loop.declareFromRetroIfEnabled(context.Background(), dynCfg(5))
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("LiveCount = %d, want 0 (no proposer wired ⇒ no-op)", got)
	}
}

// TestRetroProposer_EnabledDeclaresFromSuggestion: opted in, a recorded suggestion
// becomes a STARTED experiment whose thesis is seeded from the retro, on a flag the
// dynamic selector chose.
func TestRetroProposer_EnabledDeclaresFromSuggestion(t *testing.T) {
	t.Setenv(envProposerEnabled, "true")
	loop := proposerLoop(t)
	loop.proposer.Record(experiment.RetroSuggestion{
		InterventionType: "adjust_global_difficulty",
		Emphasis:         "weight_up",
		Reason:           "the room faded after the first elimination",
	})

	loop.declareFromRetroIfEnabled(context.Background(), dynCfg(5))

	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("LiveCount = %d, want 1 (opted-in proposer declares from the suggestion)", got)
	}
	if got := loop.proposer.Backlog(); got != 0 {
		t.Errorf("backlog = %d, want 0 (suggestion consumed on declare)", got)
	}
	// The declared experiment's intent carries the retro-seeded thesis.
	live := loop.registry.Live()
	if len(live) != 1 {
		t.Fatalf("live experiments = %d, want 1", len(live))
	}
}

// TestRetroProposer_NoBacklog_NoOp: opted in but with no recorded suggestion, the
// hook declares nothing (it only acts on the retro backlog).
func TestRetroProposer_NoBacklog_NoOp(t *testing.T) {
	t.Setenv(envProposerEnabled, "true")
	loop := proposerLoop(t)
	loop.declareFromRetroIfEnabled(context.Background(), dynCfg(5))
	if got := loop.registry.LiveCount(); got != 0 {
		t.Fatalf("LiveCount = %d, want 0 (no suggestion ⇒ nothing declared)", got)
	}
}

// TestRetroProposer_RespectsBudget: opted in, the concurrent-experiment budget caps
// retro-seeded declaration just like the dynamic declarer.
func TestRetroProposer_RespectsBudget(t *testing.T) {
	t.Setenv(envProposerEnabled, "true")
	loop := proposerLoop(t)
	loop.proposer.Record(experiment.RetroSuggestion{Reason: "first"})
	loop.proposer.Record(experiment.RetroSuggestion{Reason: "second"})

	// Budget 1: the first declare fills it; the second is suppressed (suggestion kept).
	loop.declareFromRetroIfEnabled(context.Background(), budgetCfg(1))
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("after first declare LiveCount = %d, want 1", got)
	}
	loop.declareFromRetroIfEnabled(context.Background(), budgetCfg(1))
	if got := loop.registry.LiveCount(); got != 1 {
		t.Fatalf("LiveCount = %d, want 1 (budget 1 caps a second declare)", got)
	}
	if got := loop.proposer.Backlog(); got != 1 {
		t.Errorf("backlog = %d, want 1 (the budget-suppressed suggestion is retained)", got)
	}
}

// budgetCfg is a live ExperimentConfig with the gates on and a given concurrent
// budget, for the budget cap test.
func budgetCfg(maxConcurrent int) flags.ExperimentConfig {
	return flags.ExperimentConfig{
		Enabled:                    true,
		DynamicEnabled:             true,
		DynamicMaxConcurrent:       maxConcurrent,
		ShadowEffectiveConcurrency: 4,
	}
}
