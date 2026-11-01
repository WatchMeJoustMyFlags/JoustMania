package main

import (
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/gamewindow"
	"github.com/joustmania/agent/goal"
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

// UC: UC-042 A12 BR-009
// TestGameEndChain_SummaryAndRetroSeeGoal: the Book scores the goal at the start
// of its game-end hook, so the summary and the retro, which run after it, both see
// the game's goal attainment (#2479, decide-contract.md §14.4).
func TestGameEndChain_SummaryAndRetroSeeGoal(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	book := prediction.New(ctx, nil)
	t.Cleanup(func() { cancel(); book.Await() })
	book.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
		return goal.FromObjectives(map[string]float64{"accelerate": 0.7}, 120, 60)
	})
	summaries := gamesummary.NewCoordinator(nil)
	sink := &summarySink{}
	summaries.SetObserver(sink)
	retro := decision.NewRetroCoordinator(retroFlags{}, nil)
	text := &captureText{}
	retro.SetBackend(text)
	wirePredictionLookups(book, summaries, retro)

	inactive, dur := false, 45.0
	ended := gamecontext.GameContext{SessionID: "game_goal", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{
		"A": {Serial: "A", Active: &inactive}}}
	ended.Session.GameActive = &inactive
	ended.Session.DurationSeconds = &dur
	ended.EndReason = gamecontext.EndReasonZero
	chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()), book.OnGameEnd, nil, retro.OnGameEnd, summaries.OnGameEnd, nil)
	chain(ended)
	retro.AwaitInflight()

	if len(sink.got) != 1 || sink.got[0].Goal == nil || sink.got[0].Goal.Profile != goal.NameQuickGame {
		t.Fatalf("UC-042/A12 summary goal = %+v", sink.got)
	}
	text.mu.Lock()
	defer text.mu.Unlock()
	if !strings.Contains(text.user, "Goal (profile quick_game") || !strings.Contains(text.user, "duration<=60s: 45s -> 1.00") {
		t.Errorf("UC-042/A12 the retro prompt lacks the goal block:\n%s", text.user)
	}
}

// UC: UC-042 A12 BR-009
// TestBuildGameEnd_WiresPredictionLookups: main builds the game-end side through
// buildGameEnd, so this pins that call site: when it stops calling
// wirePredictionLookups the summary and the retro read no predictions and no goal
// (#2498; the chain test above wires them itself and cannot see that).
func TestBuildGameEnd_WiresPredictionLookups(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	book := prediction.New(ctx, nil)
	t.Cleanup(func() { cancel(); book.Await() })
	book.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
		return goal.FromObjectives(map[string]float64{"accelerate": 0.7}, 120, 60)
	})
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	c := &components{
		ctx:         ctx,
		logger:      logger,
		llmBudget:   newLLMBudgetShare(nil, logger),
		window:      gamewindow.NewStore(),
		gameFlags:   flagservice.New("http://127.0.0.1:0"),
		predictions: book,
	}
	c.buildGameEnd()
	sink := &summarySink{}
	c.summaries.SetObserver(sink)

	inactive, dur := false, 45.0
	ended := gamecontext.GameContext{SessionID: "game_built", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{
		"A": {Serial: "A", Active: &inactive}}}
	ended.Session.GameActive = &inactive
	ended.Session.DurationSeconds = &dur
	ended.EndReason = gamecontext.EndReasonZero
	book.OnGameEnd(ended)
	c.summaries.OnGameEnd(ended)

	if len(sink.got) != 1 || sink.got[0].Goal == nil {
		t.Fatalf("UC-042/A12 buildGameEnd did not wire the Book's goal lookup into the summary: %+v", sink.got)
	}
}
