package decision

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/llm"
)

// promptCapture is a text backend that keeps the prompt it was asked.
type promptCapture struct {
	mu   sync.Mutex
	user string
}

func (p *promptCapture) Name() string  { return "claude" }
func (p *promptCapture) Model() string { return "claude-test" }
func (p *promptCapture) Infer(_ context.Context, pr llm.Prompt) (string, error) {
	p.mu.Lock()
	p.user = pr.User
	p.mu.Unlock()
	return `{"session_assessment":"ok","suggestions":[],"session_focus":"balanced"}`, nil
}

// UC: UC-042 A12 BR-009
// The retro reads the game's scored predictions through the lookup, by game id.
func TestRetro_PredictionsBlockFromLookup(t *testing.T) {
	run := func(t *testing.T, lookup func(string) []gamesummary.PredictionOutcome) (string, string) {
		be := &promptCapture{}
		rc, _, _, _ := budgetedRetro(t, 2, be)
		if lookup != nil {
			rc.SetPredictionLookup(lookup)
		}
		c := endedSession()
		rc.OnGameEnd(c)
		rc.AwaitInflight()
		be.mu.Lock()
		defer be.mu.Unlock()
		return be.user, c.SessionID
	}
	t.Run("UC-042/A12 a game with predictions gets the block", func(t *testing.T) {
		var asked string
		user, id := run(t, func(gameID string) []gamesummary.PredictionOutcome {
			asked = gameID
			return []gamesummary.PredictionOutcome{{ID: "p1", Backend: "rules", Source: "rules", Template: "survives", HorizonS: 15, P: 0.7, Acted: true, Outcome: "hit"}}
		})
		if asked != id {
			t.Errorf("lookup asked for %q, want %q", asked, id)
		}
		if !strings.Contains(user, "Predictions vs outcomes") || !strings.Contains(user, "rules/rules survives") {
			t.Errorf("retro prompt lacks the predictions block:\n%s", user)
		}
	})
	t.Run("UC-042/A12 without a lookup the prompt is unchanged", func(t *testing.T) {
		user, _ := run(t, nil)
		if user == "" || strings.Contains(user, "Predictions vs outcomes") {
			t.Errorf("unexpected predictions block or empty prompt:\n%s", user)
		}
	})
}
