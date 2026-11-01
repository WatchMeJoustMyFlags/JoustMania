package main

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/prediction"
)

// UC: UC-043 A21 BR-010
// TestGameEndChain_ArmCalibration: a real Book, a real registry, the game-end
// chain order and main's wiring (#2498): the Book runs before the experiment
// hook, so a shadow game's acted prediction lands on its arm before the game is
// concluded.
func TestGameEndChain_ArmCalibration(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	book := prediction.New(ctx, nil)
	t.Cleanup(func() { cancel(); book.Await() })

	spawner := &recordingSpawner{}
	reg := experiment.NewRegistry(experiment.RegistryConfig{Root: t.TempDir(), MaxShadowGames: 2, Spawner: spawner, Log: testLogger()})
	id, err := reg.Declare(experiment.Intent{
		Hypothesis: "h", FlagKey: "windows", ExperimentalValue: 0.5, Objective: "balanced", TargetNPerArm: 2,
	})
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := reg.Start(ctx, id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if n := reg.AllocateAndSpawn(ctx); n == 0 {
		t.Fatal("no game spawned")
	}
	shadow := spawner.drain()[0]
	wireArmCalibration(book, reg)
	wireArmCalibration(nil, reg) // nil-safe
	wireArmCalibration(book, nil)

	active := true
	running := gamecontext.GameContext{SessionID: shadow.gameID, GameKind: "shadow", ExperimentID: id, Arm: shadow.arm,
		Players: map[string]*gamecontext.PlayerSignals{"A": {Serial: "A", Active: &active}, "B": {Serial: "B", Active: &active}}}
	running.Session.GameActive = &active
	if _, _, ok := book.Register(prediction.Registration{Backend: "rules", Acted: true,
		Prediction: decide.NewPrediction(decide.TemplateNoElimination, 15, 0.7, decide.SourceRules, "R4", ""), Context: running}); !ok {
		t.Fatal("register failed")
	}
	// B is eliminated right after: the no_elimination prediction is an early miss.
	running.Session.EliminationSequence = []string{"B"}
	running.Timeline = []gamecontext.TimelineEvent{{At: time.Now(), Kind: gamecontext.EventElimination, Serial: "B"}}
	running.Players = map[string]*gamecontext.PlayerSignals{"A": {Serial: "A", Active: &active}, "B": {Serial: "B", Active: new(bool)}}
	concluded := false
	experimentHook := func(c gamecontext.GameContext) {
		concluded = true
		if _, err := reg.ConcludeGame(ctx, c.SessionID, 0.5, 60); err != nil {
			t.Errorf("ConcludeGame: %v", err)
		}
	}
	chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()), book.OnGameEnd, experimentHook, nil, nil, nil)
	ended := running
	inactive := false
	ended.Session.GameActive = &inactive
	ended.EndReason = gamecontext.EndReasonZero
	chain(ended)

	if !concluded {
		t.Fatal("the experiment hook did not run")
	}
	got := reg.ArmCalibration(id)[shadow.arm]
	if got.Scored != 1 || got.Hits != 0 {
		t.Fatalf("UC-043/A21 arm %s tally = %+v, want the one scored prediction", shadow.arm, got)
	}
}
