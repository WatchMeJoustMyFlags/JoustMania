package llm

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/gamesummary"
)

func gptr(v float64) *float64 { return &v }

// UC: UC-042 A12 BR-009
// The retro prompt of a game with a goal carries the "Goal" block: the profile,
// each term's band, measured value and score, and the attainment (#2479).
func TestRetroGoalBlock(t *testing.T) {
	ctx := retroThreePlayers()
	summary := gamesummary.BuildSummary(ctx, gamesummary.BuildOptions{Now: fixedNow})
	summary.Goal = &gamesummary.Goal{
		Profile: "long_running_game", FromObjectives: true, Attainment: gptr(0.75),
		Terms: []gamesummary.GoalTerm{
			{Characteristic: "duration", Phase: "whole", Min: gptr(180), Weight: 1, Value: gptr(90), Score: gptr(0.5)},
			{Characteristic: "activity", Phase: "endgame", Min: gptr(0.6), Weight: 1},
		},
	}
	user := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: ctx, Summary: summary, Now: fixedNow}).User
	for _, want := range []string{
		"\n\nGoal (profile long_running_game, derived from the objectives;",
		"): attainment 0.75\n",
		"  duration>=180s: 90s -> 0.50\n",
		"  activity@endgame>=0.60: not measured",
	} {
		if !strings.Contains(user, want) {
			t.Errorf("UC-042/A12 retro goal block lacks %q:\n%s", want, user)
		}
	}

	summary.Goal = nil
	if user := BuildRetro(RetroInput{Snapshot: retroSnapshot(), Context: ctx, Summary: summary, Now: fixedNow}).User; strings.Contains(user, "Goal (") {
		t.Error("UC-042/A12 a game without a goal must have no Goal block")
	}
}
