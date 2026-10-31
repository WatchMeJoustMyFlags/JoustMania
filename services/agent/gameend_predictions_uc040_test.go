package main

import (
	"context"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/prediction"
)

// UC: UC-040 A44 BR-034
// TestBuildGameEndChain_PredictionsScoredBeforeSummary: the real prediction book
// sits second in the game-end chain, so a game's pending predictions are scored
// before the experiment, retro and summary consumers run (#2479).
func TestBuildGameEndChain_PredictionsScoredBeforeSummary(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	book := prediction.New(ctx, nil)
	t.Cleanup(func() { cancel(); book.Await() })

	active := true
	c := gamecontext.GameContext{SessionID: "game_chain", Players: map[string]*gamecontext.PlayerSignals{
		"A": {Serial: "A", Active: &active}, "B": {Serial: "B", Active: &active}}}
	c.Session.GameActive = &active
	if _, _, ok := book.Register(prediction.Registration{Backend: "rules",
		Prediction: decide.NewPrediction(decide.TemplateSurvives, 15, 0.7, decide.SourceRules, "R4", "A"), Context: c}); !ok {
		t.Fatal("register failed")
	}

	seenAt := map[string]int{}
	look := func(name string) func(gamecontext.GameContext) {
		return func(g gamecontext.GameContext) { seenAt[name] = len(book.Results(g.SessionID)) }
	}
	chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()), book.OnGameEnd,
		look("experiment"), look("retro"), look("summary"), look("proposer"))
	end := c
	end.EndReason = gamecontext.EndReasonZero
	chain(end)
	for _, name := range []string{"experiment", "retro", "summary", "proposer"} {
		if seenAt[name] != 1 {
			t.Errorf("UC-040/A44 %s saw %d scored predictions, want 1 (the book runs first)", name, seenAt[name])
		}
	}
}
