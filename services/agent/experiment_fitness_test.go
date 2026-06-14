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

// fp / bp return pointers to a float64 / bool, distinguishing "observed" from
// "never observed" the way the gamecontext signal fields do.
func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

// concludedBalancedContext is a game-end snapshot exactly as the Store hands
// OnGameEnd for a balanced shadow game: every player has been eliminated
// (Active=false — the coordinator's game_player_alive=0), the session is over
// (GameActive=false), but each player still carries the per-player skill_level /
// movement_variance / accel_magnitude readings the coordinator emitted at ~10Hz
// (the Store never wipes Players on game end). This is the state in which balanced
// folded 0 before #1015: spike-survival saw no Active player and was skipped.
func concludedBalancedContext() gamecontext.GameContext {
	off := false
	return gamecontext.GameContext{
		SessionID:    "game_balanced_1",
		GameKind:     "shadow",
		ExperimentID: "exp_balanced",
		Arm:          "experimental",
		Session:      gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.45), MovementIntensity: fp(1.0), MovementVariance: fp(0.4)},
			"b": {Serial: "b", Active: &off, SkillLevel: fp(0.55), MovementIntensity: fp(1.0), MovementVariance: fp(0.6)},
		},
	}
}

// TestWithEffectiveBalancedSignals_RecoversAtConclusion is the #1015 root-cause
// fix for the balanced objective: at game end every player is eliminated
// (Active=false), so the spike-survival sub-check — which scores only Active
// players — was skipped, even though each player's real movement readings are
// still on the snapshot. The recovery re-admits players carrying movement signals
// so spike-survival can fold their retained readings.
func TestWithEffectiveBalancedSignals_RecoversAtConclusion(t *testing.T) {
	gc := withEffectiveBalancedSignals(concludedBalancedContext())
	active := 0
	for _, p := range gc.Players {
		if p.Active != nil && *p.Active {
			active++
		}
	}
	if active != 2 {
		t.Fatalf("expected both signal-carrying players re-admitted, got %d active", active)
	}
}

// TestWithEffectiveBalancedSignals_LiveStateIsAuthoritative ensures the recovery
// is a no-op while the game is still live (GameActive=true) or any player is still
// Active — the live participant set must win, mirroring the gauge-is-authoritative
// discipline of withEffectiveDuration.
func TestWithEffectiveBalancedSignals_LiveStateIsAuthoritative(t *testing.T) {
	t.Run("game still active", func(t *testing.T) {
		gc := concludedBalancedContext()
		gc.Session.GameActive = bp(true)
		got := withEffectiveBalancedSignals(gc)
		for _, p := range got.Players {
			if p.Active != nil && *p.Active {
				t.Fatal("live game must not have players re-admitted")
			}
		}
	})
	t.Run("a player still active", func(t *testing.T) {
		gc := concludedBalancedContext()
		gc.Players["a"].Active = bp(true)
		got := withEffectiveBalancedSignals(gc)
		// b stays inactive (the live participant set — only a — is authoritative).
		if got.Players["b"].Active != nil && *got.Players["b"].Active {
			t.Fatal("recovery must not run while a player is still Active")
		}
	})
}

// TestWithEffectiveBalancedSignals_NoFabrication ensures a player without the
// movement signals is never re-admitted — the recovery only re-admits players
// whose own real readings already sit on the snapshot.
func TestWithEffectiveBalancedSignals_NoFabrication(t *testing.T) {
	off := false
	gc := gamecontext.GameContext{
		Session: gamecontext.SessionSignals{GameActive: &off},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": {Serial: "a", Active: &off, SkillLevel: fp(0.5)}, // no movement signals
		},
	}
	got := withEffectiveBalancedSignals(gc)
	if got.Players["a"].Active != nil && *got.Players["a"].Active {
		t.Fatal("a signal-less player must not be re-admitted (no fabrication)")
	}
}

// TestOnGameEnd_DrivenBalancedGameYieldsNonZeroFitness is the headline #1015
// acceptance: a balanced shadow game driven to a natural end — every player
// eliminated, but each carrying its real per-player skill/movement readings —
// concludes with a MEANINGFUL balanced fitness in (0,1] AND both sub-signals
// (skill_gap + spike_survival) present, not the worst-case 0 that made balanced
// experiments inert (issue #1015).
func TestOnGameEnd_DrivenBalancedGameYieldsNonZeroFitness(t *testing.T) {
	loop := &experimentLoop{
		registry: nil, // not exercised: we score fitness directly via the same path onGameEnd uses
		log:      testLogger(),
		fitness:  defaultGameFitness,
	}

	// Mirror onGameEnd's recovery + scoring without the registry side effects.
	scored := withEffectiveBalancedSignals(concludedBalancedContext())
	fit := loop.fitness(scored, decision.ObjectiveBalanced)
	if fit <= 0 {
		t.Fatalf("driven balanced game must yield non-zero balanced fitness, got %v", fit)
	}

	// Both sub-signals must have been computed (the whole point of #1015).
	eval := decision.EvaluateFitness(scored, decision.DefaultStaticConfig().FitnessThresholds)
	r, ok := eval.Results[decision.ObjectiveBalanced]
	if !ok || !r.Evaluated {
		t.Fatal("balanced must be evaluated at conclusion")
	}
	if _, ok := r.Values["balanced.skill_gap"]; !ok {
		t.Error("skill_gap sub-signal must be present")
	}
	if _, ok := r.Values["balanced.spike_survival_ratio"]; !ok {
		t.Error("spike_survival sub-signal must be present (the #1015 recovery)")
	}
}
