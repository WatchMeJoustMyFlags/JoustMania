package prediction

import (
	"context"
	"math"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/goal"
)

// respawn_mode_uc040_test.go: in a respawn mode (NonStop, Zombies, Swapper) a
// death keeps the player in the game, so the elimination sequence stays empty
// however many deaths there are (#2524). The elimination-based templates and the
// elimination_pace goal measure would then always miss (elimination), always hit
// (no_elimination, survives) or score 0, so they are not scored there (#2532,
// UC-040 BR-043).

// respawnGame is a running game of mode after many respawn deaths: every player
// died and came back, so nobody is in the elimination sequence or the timeline.
func respawnGame(mode string) gamecontext.GameContext {
	c := game("P1", "P2", "P3", "P4")
	c.Session.GameMode = &mode
	return c
}

// UC: UC-040 A44 BR-043
func TestBook_RespawnModeEliminationTemplatesAreUnscorable(t *testing.T) {
	for _, mode := range []string{"Nonstop Joust", "Zombie", "Swapper"} {
		t.Run("UC-040/BR-043 "+mode+": elimination at the horizon with 0 eliminations is unscorable, not a miss", func(t *testing.T) {
			h := newHarness(t)
			c := respawnGame(mode)
			h.register(pred(decide.TemplateElimination, 30, ""), c, true)
			h.at(33 * time.Second)
			h.setLive(&c)
			h.fire()
			r := h.one(h.end(c))
			if r.Outcome != OutcomeUnscorable || r.UnscorableReason != ReasonRespawnMode {
				t.Fatalf("result = %s/%s, want unscorable/%s", r.Outcome, r.UnscorableReason, ReasonRespawnMode)
			}
		})
		t.Run("UC-040/BR-043 "+mode+": no_elimination and survives are unscorable, not a hit", func(t *testing.T) {
			h := newHarness(t)
			c := respawnGame(mode)
			h.register(pred(decide.TemplateNoElimination, 15, ""), c, true)
			h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
			h.at(18 * time.Second)
			h.setLive(&c)
			h.fire()
			for _, r := range h.end(c) {
				if r.Outcome != OutcomeUnscorable || r.UnscorableReason != ReasonRespawnMode {
					t.Fatalf("%s = %s/%s, want unscorable/%s", r.Prediction.Template, r.Outcome,
						r.UnscorableReason, ReasonRespawnMode)
				}
			}
		})
	}
	t.Run("UC-040/BR-043 the first decision cycle that sees the mode scores it", func(t *testing.T) {
		h := newHarness(t)
		c := game("P1", "P2", "P3") // mode not observed yet at registration
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(2 * time.Second)
		live := respawnGame("Nonstop Joust")
		h.b.Observe(live)
		if h.b.Pending() != 0 {
			t.Fatalf("pending = %d, want 0: scored at the first cycle that sees the mode", h.b.Pending())
		}
		r := h.one(h.end(live))
		if r.Outcome != OutcomeUnscorable || r.UnscorableReason != ReasonRespawnMode {
			t.Fatalf("result = %s/%s", r.Outcome, r.UnscorableReason)
		}
	})
	t.Run("UC-040/BR-043 a Swapper disconnect elimination does not make it a hit", func(t *testing.T) {
		h := newHarness(t)
		c := respawnGame("Swapper")
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(5 * time.Second)
		r := h.one(h.end(eliminate(c, "P4", 4*time.Second)))
		if r.Outcome != OutcomeUnscorable || r.UnscorableReason != ReasonRespawnMode {
			t.Fatalf("result = %s/%s", r.Outcome, r.UnscorableReason)
		}
	})
	t.Run("UC-040/BR-043 activity, closeness and game_ends still score in a respawn mode", func(t *testing.T) {
		h := newHarness(t)
		c := respawnGame("Nonstop Joust")
		h.register(pred(decide.TemplateGameEnds, 30, ""), c, true)
		h.at(5 * time.Second)
		want(t, h.one(h.end(c)), OutcomeHit, "", true)
	})
	t.Run("UC-040/BR-043 FFA is unchanged (positive control)", func(t *testing.T) {
		h := newHarness(t)
		c := respawnGame("FFA")
		h.register(pred(decide.TemplateElimination, 30, ""), c, true)
		h.at(33 * time.Second)
		h.setLive(&c)
		h.fire()
		want(t, h.one(h.end(c)), OutcomeMiss, "", false)
	})
}

// UC: UC-042 A12 BR-009
// UC: UC-040 BR-043
func TestBook_RespawnModeGoalLeavesEliminationPaceOut(t *testing.T) {
	quick := func(context.Context, gamecontext.GameContext) goal.Profile {
		hi, lo := 120.0, 2.0
		return goal.Profile{Name: "quick_game", Terms: []goal.Term{
			{Characteristic: decide.CharDuration, Max: &hi, Phase: goal.PhaseWhole, Weight: 1},
			{Characteristic: decide.CharEliminationPace, Min: &lo, Phase: goal.PhaseWhole, Weight: 1},
		}}
	}
	score := func(t *testing.T, mode string) *float64 {
		t.Helper()
		h := newHarness(t)
		h.b.SetGoalResolver(quick)
		end := respawnGame(mode)
		end.Session.DurationSeconds = fp(90)
		end.EndReason = gamecontext.EndReasonZero
		h.b.OnGameEnd(end)
		g := h.b.SummaryGoal("g1")
		if g == nil || g.Attainment == nil {
			t.Fatalf("goal = %+v", g)
		}
		return g.Attainment
	}
	t.Run("UC-042/A12 NonStop 90 s with 0 eliminations: only the duration term is measured", func(t *testing.T) {
		if a := score(t, "Nonstop Joust"); math.Abs(*a-1) > 1e-9 {
			t.Fatalf("attainment = %v, want 1 (elimination_pace left out, duration 90 <= 120)", *a)
		}
	})
	t.Run("UC-042/A12 FFA 90 s with 0 eliminations still scores elimination_pace (positive control)", func(t *testing.T) {
		if a := score(t, "FFA"); math.Abs(*a-0.5) > 1e-9 {
			t.Fatalf("attainment = %v, want 0.5 (pace 0 of >= 2 scores 0)", *a)
		}
	})
}
