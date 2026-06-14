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
// (GameActive=false), but each player still carries the per-player skill_level it
// reported while alive (the Store sets SkillLevel from game_player_skill_level and
// never wipes Players — or their SkillLevel — on game end; see the realistic
// ingest proof in gamecontext: TestGroundTruth_SkillLevelSurvivesToConclusionSnapshot).
//
// #1015 rework note: balanced's skill-gap sub-check reads c.Players DIRECTLY (it is
// NOT Active-gated; see decision.skillGap), so it computes at conclusion from these
// retained SkillLevels with no recovery step needed. The earlier premise that
// "balanced folds 0 at conclusion because every player is eliminated" was false —
// only spike-survival is Active-gated, and it is honestly skipped at conclusion
// (its inputs are frozen at the last live frame and not meaningful post-game).
func concludedBalancedContext() gamecontext.GameContext {
	off := false
	return gamecontext.GameContext{
		SessionID:    "game_balanced_1",
		GameKind:     "shadow",
		ExperimentID: "exp_balanced",
		Arm:          "experimental",
		Session:      gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.45)},
			"b": {Serial: "b", Active: &off, SkillLevel: fp(0.55)},
		},
	}
}

// TestOnGameEnd_ConcludedBalancedGameFoldsNonZeroFromSkillGap is the corrected
// #1015 acceptance. It asserts the BEHAVIORAL truth established by ground-truth
// tracing: a concluded balanced shadow game in which >=2 eliminated players still
// carry their retained skill_level folds a MEANINGFUL, non-zero balanced fitness
// straight from the skill-gap sub-check — no Active-recovery needed, because
// skill-gap reads c.Players directly. (A two-player skill gap of 0.10 against the
// default max_skill_gap 0.4 scores 0.875.) This is the exact path onGameEnd uses.
func TestOnGameEnd_ConcludedBalancedGameFoldsNonZeroFromSkillGap(t *testing.T) {
	loop := &experimentLoop{
		registry: nil, // not exercised: we score fitness directly via the same path onGameEnd uses
		log:      testLogger(),
		fitness:  defaultGameFitness,
	}

	// onGameEnd applies withEffectiveDuration only; balanced needs no signal recovery.
	scored := withEffectiveDuration(concludedBalancedContext())
	fit := loop.fitness(scored, decision.ObjectiveBalanced)
	if fit <= 0 {
		t.Fatalf("concluded balanced game with >=2 skill_level players must fold non-zero, got %v", fit)
	}

	eval := decision.EvaluateFitness(scored, decision.DefaultStaticConfig().FitnessThresholds)
	r, ok := eval.Results[decision.ObjectiveBalanced]
	if !ok || !r.Evaluated {
		t.Fatal("balanced must be evaluated at conclusion (skill-gap computes)")
	}
	if _, ok := r.Values["balanced.skill_gap"]; !ok {
		t.Error("skill_gap sub-signal must be present — it is the non-Active-gated driver")
	}
	// spike-survival is honestly absent at conclusion: it is Active-gated and every
	// player has been eliminated. This is correct (its inputs would be frozen at the
	// last live frame), NOT a bug to be patched by re-admitting dead players.
	if _, ok := r.Values["balanced.spike_survival_ratio"]; ok {
		t.Error("spike_survival must NOT be present at conclusion (Active-gated; honestly skipped)")
	}
}

// TestBalanced_FoldsZeroOnlyWhenSkillLevelGenuinelyMissing is the contrast test
// that would have caught the original false premise: balanced folds the worst-case
// 0 ONLY when fewer than two players carry a skill_level (so skill-gap cannot
// compute) AND no Active player has movement signals (so spike-survival is skipped
// too). With skill_level present on >=2 players, balanced is never 0 at conclusion.
func TestBalanced_FoldsZeroOnlyWhenSkillLevelGenuinelyMissing(t *testing.T) {
	off := false
	// Only ONE player carries a skill level: skill-gap needs >=2, so it is skipped;
	// no Active player, so spike-survival is skipped → balanced computes nothing → 0.
	gc := gamecontext.GameContext{
		Session: gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.5)},
			"b": {Serial: "b", Active: &off}, // no skill_level
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
