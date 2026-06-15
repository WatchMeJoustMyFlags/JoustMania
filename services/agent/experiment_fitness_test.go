package main

import (
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
)

// TestDefaultGameFitness_DurationlessEnduranceFoldsZero pins the #994 contract that
// the worst-case 0 is hit ONLY for a genuinely signal-less game: an endurance game
// whose GameContext carries no duration AND no timeline to recover one folds 0.
func TestDefaultGameFitness_DurationlessEnduranceFoldsZero(t *testing.T) {
	gc := gamecontext.GameContext{
		SessionID:    "game_abc123",
		ExperimentID: "exp_1",
		Arm:          "experimental",
		// No Session.DurationSeconds, no Timeline → nothing to recover.
	}
	gc = withEffectiveDuration(gc)
	if gc.Session.DurationSeconds != nil {
		t.Fatalf("expected DurationSeconds to stay nil for a signal-less game, got %v", *gc.Session.DurationSeconds)
	}
	if fit := defaultGameFitness(gc, decision.ObjectiveEndurance); fit != 0 {
		t.Fatalf("signal-less endurance game must fold worst-case 0, got %v", fit)
	}
}

// TestWithEffectiveDuration_RecoversFromTimeline is the #997 root-cause fix: when the
// coordinator's end-only game_duration_seconds gauge has not arrived (DurationSeconds
// nil) but the partition's start→end phase timeline is present, the real elapsed game
// time is recovered onto the context.
func TestWithEffectiveDuration_RecoversFromTimeline(t *testing.T) {
	start := time.Unix(1_000, 0)
	gc := gamecontext.GameContext{
		SessionID: "game_abc123",
		Timeline: []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "start"},
			{At: start.Add(45 * time.Second), Kind: gamecontext.EventPhase, Detail: "end"},
		},
	}
	got := withEffectiveDuration(gc)
	if got.Session.DurationSeconds == nil {
		t.Fatal("expected DurationSeconds to be recovered from the timeline, got nil")
	}
	if *got.Session.DurationSeconds != 45 {
		t.Fatalf("recovered duration = %v, want 45", *got.Session.DurationSeconds)
	}
}

// TestWithEffectiveDuration_GaugeIsAuthoritative ensures the recovery never clobbers a
// real game_duration_seconds gauge when it IS present (the gauge wins).
func TestWithEffectiveDuration_GaugeIsAuthoritative(t *testing.T) {
	gauge := 120.0
	start := time.Unix(1_000, 0)
	gc := gamecontext.GameContext{
		Session: gamecontext.SessionSignals{DurationSeconds: &gauge},
		Timeline: []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "start"},
			{At: start.Add(45 * time.Second), Kind: gamecontext.EventPhase, Detail: "end"},
		},
	}
	got := withEffectiveDuration(gc)
	if got.Session.DurationSeconds == nil || *got.Session.DurationSeconds != 120 {
		t.Fatalf("gauge must remain authoritative, got %v", got.Session.DurationSeconds)
	}
}

// TestWithEffectiveDuration_NoBracketsNoRecovery ensures a timeline missing a phase
// bracket (or with a non-positive span) recovers nothing, preserving the
// signal-less-folds-zero contract.
func TestWithEffectiveDuration_NoBracketsNoRecovery(t *testing.T) {
	start := time.Unix(1_000, 0)
	cases := []struct {
		name     string
		timeline []gamecontext.TimelineEvent
	}{
		{"start only", []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "start"},
		}},
		{"end only", []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "end"},
		}},
		{"non-positive span", []gamecontext.TimelineEvent{
			{At: start.Add(10 * time.Second), Kind: gamecontext.EventPhase, Detail: "start"},
			{At: start, Kind: gamecontext.EventPhase, Detail: "end"},
		}},
		{"empty", nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gc := gamecontext.GameContext{Timeline: tc.timeline}
			got := withEffectiveDuration(gc)
			if got.Session.DurationSeconds != nil {
				t.Fatalf("expected no recovery, got %v", *got.Session.DurationSeconds)
			}
		})
	}
}

// TestOnGameEnd_DrivenShadowGameYieldsNonZeroFitness is the headline #997 acceptance:
// a shadow game driven to a natural end (no end-only duration gauge observed, but a
// start→end phase timeline present, exactly as the Store hands OnGameEnd) concludes
// with a MEANINGFUL, objective-relevant endurance fitness in (0,1] — not the
// worst-case 0 that made every concluded game inert in the M8 dry run.
func TestOnGameEnd_DrivenShadowGameYieldsNonZeroFitness(t *testing.T) {
	// A 60s game against the default 120s endurance target → progress 0.5.
	start := time.Unix(1_000, 0)
	gc := gamecontext.GameContext{
		SessionID:    "game_driven_1",
		GameKind:     "shadow",
		ExperimentID: "exp_endurance",
		Arm:          "experimental",
		Timeline: []gamecontext.TimelineEvent{
			{At: start, Kind: gamecontext.EventPhase, Detail: "start"},
			{At: start.Add(60 * time.Second), Kind: gamecontext.EventPhase, Detail: "end"},
		},
	}

	loop := &experimentLoop{
		registry: nil, // not exercised: we score fitness directly via the same path onGameEnd uses
		log:      testLogger(),
		fitness:  defaultGameFitness,
	}

	// Mirror onGameEnd's signal-recovery + scoring without the registry side effects.
	scored := withEffectiveDuration(gc)
	fit := loop.fitness(scored, decision.ObjectiveEndurance)
	if fit <= 0 {
		t.Fatalf("driven shadow game must yield non-zero endurance fitness, got %v", fit)
	}
	if fit != 0.5 {
		t.Fatalf("60s/120s endurance progress = %v, want 0.5", fit)
	}
}

// fp returns a pointer to a float64, distinguishing "observed" from "never
// observed" the way the gamecontext signal fields do.
func fp(v float64) *float64 { return &v }

// concludedBalancedContext is a game-end snapshot exactly as the Store hands
// OnGameEnd for a balanced shadow game: every player has been eliminated
// (Active=false — the coordinator's game_player_alive=0), the session is over
// (GameActive=false), but each player still carries the per-player signals it
// reported while alive. The Store sets SkillLevel from game_player_skill_level and
// — as of #1024 — PeakAccel + MovementVarianceAggregate from the retained
// whole-game movement aggregates, and never wipes Players (or these retained
// fields) on game end (see the realistic ingest proofs in gamecontext:
// TestGroundTruth_SkillLevelSurvivesToConclusionSnapshot and
// TestStore_MovementAggregatesSurviveToConclusionSnapshot).
//
// Balanced binds on skill-gap (reads c.Players DIRECTLY; never Active-gated; see
// decision.skillGap). The retained whole-game aggregates (PeakAccel +
// MovementVarianceAggregate) feed the spike-survival ratio, which is RECORDED for
// observability/#1125 but does NOT gate balanced.
func concludedBalancedContext() gamecontext.GameContext {
	off := false
	return gamecontext.GameContext{
		SessionID:    "game_balanced_1",
		GameKind:     "shadow",
		ExperimentID: "exp_balanced",
		Arm:          "experimental",
		Session:      gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			// Retained whole-game aggregates: std=sqrt(varAgg) <= 0.30*peak for both
			// (peak 2.0 → survive bound varAgg <= 0.36) → both survive spikes →
			// survival_ratio 1.0.
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.45), PeakAccel: fp(2.0), MovementVarianceAggregate: fp(0.09)}, // std 0.3 <= 0.6
			"b": {Serial: "b", Active: &off, SkillLevel: fp(0.55), PeakAccel: fp(2.0), MovementVarianceAggregate: fp(0.16)}, // std 0.4 <= 0.6
		},
	}
}

// TestOnGameEnd_ConcludedBalancedGameFoldsNonZeroFromSkillGapAtConclusion is the
// #1024 Option-2 acceptance. It asserts the balanced contract: a concluded
// balanced shadow game in which >=2 eliminated players still carry their retained
// skill_level folds a MEANINGFUL, non-zero balanced fitness from SKILL-GAP ALONE
// (reads c.Players directly; never Active-gated). The spike-survival ratio is
// RECORDED on the span from the retained whole-game aggregates (the data
// foundation for #1125) but does NOT contribute to balanced progress/satisfied.
// This is the exact path onGameEnd uses.
func TestOnGameEnd_ConcludedBalancedGameFoldsNonZeroFromSkillGapAtConclusion(t *testing.T) {
	loop := &experimentLoop{
		registry: nil, // not exercised: we score fitness directly via the same path onGameEnd uses
		log:      testLogger(),
		fitness:  defaultGameFitness,
	}

	// onGameEnd applies withEffectiveDuration only; balanced needs no signal recovery.
	scored := withEffectiveDuration(concludedBalancedContext())
	fit := loop.fitness(scored, decision.ObjectiveBalanced)
	if fit <= 0 {
		t.Fatalf("concluded balanced game must fold non-zero from skill-gap at conclusion, got %v", fit)
	}

	eval := decision.EvaluateFitness(scored, decision.DefaultStaticConfig().FitnessThresholds)
	r, ok := eval.Results[decision.ObjectiveBalanced]
	if !ok || !r.Evaluated {
		t.Fatal("balanced must be evaluated at conclusion")
	}

	// Binding driver: skill-gap (never Active-gated). Progress derives from it alone.
	gapProgress, ok := r.Values["balanced.skill_gap_progress"]
	if !ok {
		t.Fatal("skill_gap sub-signal must be present — the binding driver")
	}
	if gapProgress <= 0 {
		t.Errorf("skill-gap must fold non-zero, got progress %v", gapProgress)
	}
	if r.Progress != gapProgress {
		t.Errorf("balanced progress %v must equal skill_gap_progress %v (skill-gap binds only)", r.Progress, gapProgress)
	}

	// Spike-survival: RECORDED from the retained whole-game aggregates (#1125
	// foundation) but NON-BINDING — present in values, does not change satisfied.
	ratio, present := r.Values["balanced.spike_survival_ratio"]
	if !present {
		t.Fatal("spike_survival_ratio MUST be recorded at conclusion for observability/#1125")
	}
	if ratio != 1.0 {
		t.Errorf("both players survive (std <= 0.30*peak) → recorded ratio 1.0, got %v", ratio)
	}
	if _, present := r.Values["balanced.spike_survival_progress"]; !present {
		t.Fatal("spike_survival_progress MUST be recorded at conclusion for observability/#1125")
	}
}

// TestBalanced_FoldsZeroOnlyWhenSkillLevelGenuinelyMissing is the contrast test:
// balanced folds the worst-case 0 ONLY when fewer than two players carry a
// skill_level (so skill-gap cannot compute) AND no player carries the retained
// movement aggregates (so spike-survival is skipped too). With skill_level present
// on >=2 players, balanced is never 0 at conclusion.
func TestBalanced_FoldsZeroOnlyWhenSkillLevelGenuinelyMissing(t *testing.T) {
	off := false
	// Only ONE player carries a skill level: skill-gap needs >=2, so it is skipped;
	// no player carries PeakAccel/MovementVarianceAggregate, so spike-survival is
	// skipped → balanced computes nothing → 0.
	gc := gamecontext.GameContext{
		Session: gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.5)},
			"b": {Serial: "b", Active: &off}, // no skill_level, no aggregates
		},
	}
	if fit := defaultGameFitness(gc, decision.ObjectiveBalanced); fit != 0 {
		t.Fatalf("balanced with <2 skill_level players and no active movers must fold 0, got %v", fit)
	}

	// Add skill_level to the second player → skill-gap computes → non-zero.
	gc.Players["b"].SkillLevel = fp(0.6)
	if fit := defaultGameFitness(gc, decision.ObjectiveBalanced); fit <= 0 {
		t.Fatalf("balanced with 2 skill_level players must fold non-zero from skill-gap, got %v", fit)
	}
}
