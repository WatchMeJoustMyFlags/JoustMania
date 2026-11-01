package main

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/llm"
	"github.com/joustmania/agent/prediction"
)

type retroFlags struct{}

func (retroFlags) Evaluate(context.Context) flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		InterventionsAllowed: []string{"noop", "grant_shield"},
		LLMGate:              flags.LLMGate{EligibleGameKinds: []string{"real"}, MaxRequestsPerMinute: 6},
	}
}

type captureText struct {
	mu   sync.Mutex
	user string
}

func (*captureText) Name() string  { return "claude" }
func (*captureText) Model() string { return "claude-test" }
func (c *captureText) Infer(_ context.Context, p llm.Prompt) (string, error) {
	c.mu.Lock()
	c.user = p.User
	c.mu.Unlock()
	return `{"session_assessment":"ok","suggestions":[],"session_focus":"balanced"}`, nil
}

type summarySink struct{ got []gamesummary.Summary }

func (s *summarySink) Record(sm gamesummary.Summary) { s.got = append(s.got, sm) }

// UC: UC-042 A12
// TestGameEndChain_SummaryAndRetroSeePredictions: a real Book, the real game-end
// chain and the wiring main uses: the summary and the retro, which run after the
// Book, both see the game's scored predictions (#2479).
func TestGameEndChain_SummaryAndRetroSeePredictions(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	book := prediction.New(ctx, nil)
	t.Cleanup(func() { cancel(); book.Await() })

	summaries := gamesummary.NewCoordinator(nil)
	sink := &summarySink{}
	summaries.SetObserver(sink)
	retro := decision.NewRetroCoordinator(retroFlags{}, nil)
	text := &captureText{}
	retro.SetBackend(text)
	wirePredictionLookups(book, summaries, retro)

	active := true
	running := gamecontext.GameContext{SessionID: "game_wiring", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{
		"A": {Serial: "A", Active: &active}, "B": {Serial: "B", Active: &active}}}
	running.Session.GameActive = &active
	if _, _, ok := book.Register(prediction.Registration{Backend: "rules", Acted: true,
		Prediction: decide.NewPrediction(decide.TemplateSurvives, 15, 0.7, decide.SourceRules, "R4", "A"), Context: running}); !ok {
		t.Fatal("register failed")
	}

	chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()), book.OnGameEnd, nil, retro.OnGameEnd, summaries.OnGameEnd, nil)
	ended := running
	inactive := false
	ended.Session.GameActive = &inactive
	ended.EndReason = gamecontext.EndReasonZero
	chain(ended)
	retro.AwaitInflight()

	if len(sink.got) != 1 || len(sink.got[0].Predictions) != 1 || sink.got[0].Predictions[0].Template != decide.TemplateSurvives {
		t.Fatalf("UC-042/A12 summary predictions = %+v, want the one scored survives", sink.got)
	}
	text.mu.Lock()
	defer text.mu.Unlock()
	if !strings.Contains(text.user, "Predictions vs outcomes") || !strings.Contains(text.user, "rules/rules survives") {
		t.Errorf("UC-042/A12 the retro prompt lacks the predictions block:\n%s", text.user)
	}
}
