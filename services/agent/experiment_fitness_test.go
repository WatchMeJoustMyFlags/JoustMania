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
