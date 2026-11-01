package decision

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/goal"
	"github.com/joustmania/agent/telemetry/keys"
)

// UC: UC-042 A12 BR-009
// goal_lines_fr201_test.go: a claude or openai request carries the goal lines
// only under decision.goal_in_prompt or the neutral profile (#2479, FR-201,
// decide-contract.md §14.4). Under v1 with goal_in_prompt off the request, and so
// the prompt bytes, are unchanged.

func testGoalProfile() goal.Profile {
	return goal.FromObjectives(map[string]float64{"endurance": 0.7, "balanced": 0.1}, 180, 60)
}

func TestRemote_GoalLines(t *testing.T) {
	run := func(t *testing.T, goalInPrompt bool, profile string, backend string) (decide.Request, string, bool) {
		t.Helper()
		snap := remoteSnapshot(time.Second)
		snap.Decision.GoalInPrompt = goalInPrompt
		if backend != "" {
			snap.Decision.Backend = backend
		}
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "shield"))
		if backend != "" {
			be.name = backend
			snap.Decision.Jev = snap.Decision.Claude
			snap.Decision.OpenAI = snap.Decision.Claude
		}
		h := newRemoteHarness(t, snap, be)
		h.loop.SetPromptProfileSource(func(context.Context, string) string { return profile })
		h.loop.SetGoalSource(func(_ context.Context, c gamecontext.GameContext) goal.Profile {
			if c.SessionID != remoteGameID {
				t.Errorf("goal read for %q", c.SessionID)
			}
			return testGoalProfile()
		})
		span := h.run(t)
		v, ok := attrValue(span, keys.AttrDecideGoalProfile)
		return be.reqs[0], v.AsString(), ok
	}
	t.Run("UC-042/A12 v1 with goal_in_prompt off: no lines, the prompt bytes unchanged", func(t *testing.T) {
		req, _, ok := run(t, false, "v1", "")
		if req.Goal != nil || ok {
			t.Fatalf("goal lines %+v, span attribute %v", req.Goal, ok)
		}
		bare := req
		bare.Goal = nil
		if got, want := llmio.Render(req), llmio.Render(bare); got != want {
			t.Fatal("the v1 prompt changed")
		}
	})
	t.Run("UC-042/A12 goal_in_prompt on adds the lines under v1", func(t *testing.T) {
		req, attr, ok := run(t, true, "v1", "")
		if req.Goal == nil || !ok || attr != goal.NameLongRunningGame {
			t.Fatalf("goal lines %+v, span %q (%v)", req.Goal, attr, ok)
		}
		if req.Goal.Goal != "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min" {
			t.Fatalf("goal line = %q", req.Goal.Goal)
		}
		if !strings.HasPrefix(req.Goal.Now, "duration=") {
			t.Fatalf("now line = %q", req.Goal.Now)
		}
	})
	t.Run("UC-042/A12 neutral implies the lines", func(t *testing.T) {
		req, attr, ok := run(t, false, "neutral", "")
		if req.Goal == nil || !ok || attr != goal.NameLongRunningGame {
			t.Fatalf("goal lines %+v, span %q (%v)", req.Goal, attr, ok)
		}
		if p := llmio.Render(req); strings.Contains(p.System, "closer, more dramatic") || !strings.Contains(p.User, "\ngoal: long_running_game ") {
			t.Fatal("neutral must carry the goal line and no prose goal")
		}
	})
	t.Run("UC-042/A12 jev gets no goal lines (no goal criterion yet)", func(t *testing.T) {
		req, _, ok := run(t, true, "neutral", flags.DecisionBackendJev)
		if req.Goal != nil || ok {
			t.Fatalf("jev goal lines %+v, span attribute %v", req.Goal, ok)
		}
	})
	t.Run("UC-042/A12 the goal lines change neither the state nor the questions", func(t *testing.T) {
		with, _, _ := run(t, true, "v1", "")
		without, _, _ := run(t, false, "v1", "")
		if with.State.Digest != without.State.Digest || !reflect.DeepEqual(with.Questions, without.Questions) {
			t.Fatal("goal lines must only change the words a backend renders")
		}
	})
}

func TestGoalLines_NoTermsNoLines(t *testing.T) {
	l := &Loop{}
	l.SetGoalSource(func(context.Context, gamecontext.GameContext) goal.Profile {
		return goal.FromObjectives(map[string]float64{"endurance": 0.25, "balanced": 0.25}, 120, 60) // mixed
	})
	snap := remoteSnapshot(time.Second)
	snap.Decision.GoalInPrompt = true
	if got := l.goalLines(context.Background(), snap, flags.DecisionBackendClaude, decide.Request{}, gamecontext.GameContext{}); got != nil {
		t.Fatalf("UC-042/A12 a profile without terms (mixed) must render no lines, got %+v", got)
	}
}
