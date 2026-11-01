package prediction

import (
	"context"
	"math"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/goal"
)

// UC: UC-042 A12 BR-009
// goal_attainment_uc042_test.go: the Book samples a running game's goal
// measurements and scores the game against its goal profile once at game end,
// with the same characteristics functions as the predictions (#2479, FR-201,
// decide-contract.md §14.4).

func goalHistograms(t *testing.T, h *harness) []metricdata.HistogramDataPoint[float64] {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := h.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var out []metricdata.HistogramDataPoint[float64]
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name == MetricGoalAttainment {
				out = append(out, m.Data.(metricdata.Histogram[float64]).DataPoints...)
			}
		}
	}
	return out
}

// withGoalHistory gives every alive player of c 1 s movement samples of intensity up
// to the harness clock, and sets the game duration.
func withGoalHistory(c gamecontext.GameContext, now time.Time, durS, intensity float64) gamecontext.GameContext {
	c.Session.DurationSeconds = fp(durS)
	players := map[string]*gamecontext.PlayerSignals{}
	for s, p := range c.Players {
		cp := *p
		cp.History = nil
		for i := 15; i >= 0; i-- {
			cp.History = append(cp.History, gamecontext.MovementSample{At: now.Add(-time.Duration(i) * time.Second), Intensity: intensity})
		}
		players[s] = &cp
	}
	c.Players = players
	return c
}

func durationTerm(lo float64) goal.Term {
	return goal.Term{Characteristic: decide.CharDuration, Min: &lo, Phase: goal.PhaseWhole, Weight: 1}
}

func TestBook_GoalAttainment(t *testing.T) {
	t.Run("UC-042/A12 sampled activity and the final duration score the game once", func(t *testing.T) {
		h := newHarness(t)
		resolved := 0
		hi := 0.6
		h.b.SetGoalResolver(func(_ context.Context, c gamecontext.GameContext) goal.Profile {
			resolved++
			return goal.Profile{Name: "long_running_game", Terms: []goal.Term{
				durationTerm(180),
				{Characteristic: decide.CharActivity, Max: &hi, Phase: goal.PhaseWhole, Weight: 1},
			}}
		})
		c := game("A", "B", "C")
		h.at(20 * time.Second)
		h.b.SampleGoal(withGoalHistory(c, h.clk.now(), 20, 0.8)) // intensity 0.8 / threshold 2 = 0.4
		end := withGoalHistory(c, h.clk.now(), 90, 0.8)
		end.EndReason = gamecontext.EndReasonZero
		h.b.OnGameEnd(end)
		h.b.OnGameEnd(end) // a second end: no second score
		g := h.b.SummaryGoal("g1")
		if g == nil || g.Profile != "long_running_game" || g.Attainment == nil {
			t.Fatalf("goal = %+v", g)
		}
		// duration 90 of >= 180 -> 0.5; activity 0.4 <= 0.6 -> 1; mean 0.75.
		if math.Abs(*g.Attainment-0.75) > 1e-9 || math.Abs(*g.Terms[1].Value-0.4) > 1e-9 {
			t.Fatalf("attainment = %v, terms %+v", *g.Attainment, g.Terms)
		}
		if resolved != 1 {
			t.Fatalf("resolved %d times, want once", resolved)
		}
		points := goalHistograms(t, h)
		if len(points) != 1 || points[0].Count != 1 || math.Abs(points[0].Sum-0.75) > 1e-9 {
			t.Fatalf("%s = %+v", MetricGoalAttainment, points)
		}
		if v, _ := points[0].Attributes.Value(attribute.Key("profile")); v.AsString() != "long_running_game" {
			t.Fatalf("profile label = %q", v.AsString())
		}
		if v, _ := points[0].Attributes.Value(attribute.Key("source")); v.AsString() != goal.SourceNamed {
			t.Fatalf("source label = %q", v.AsString())
		}
	})
	t.Run("UC-042/A12 a game the Book never sampled still gets its duration term", func(t *testing.T) {
		h := newHarness(t)
		h.b.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
			return goal.Profile{Name: "quick", Terms: []goal.Term{durationTerm(60)}}
		})
		end := game("A")
		end.Session.DurationSeconds = fp(120)
		end.EndReason = gamecontext.EndReasonZero
		h.b.OnGameEnd(end)
		if g := h.b.SummaryGoal("g1"); g == nil || *g.Attainment != 1 {
			t.Fatalf("goal = %+v", g)
		}
		if h.b.Results("g1") != nil {
			t.Fatal("a goal must not change the prediction bookkeeping")
		}
	})
	t.Run("UC-042/A12 a stale end, a mixed profile and no resolver record no goal", func(t *testing.T) {
		h := newHarness(t)
		h.b.OnGameEnd(game("A")) // no resolver
		if h.b.SummaryGoal("g1") != nil {
			t.Fatal("no resolver must record no goal")
		}
		h.b.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
			return goal.FromObjectives(map[string]float64{"endurance": 0.25, "chaos": 0.25}, 120, 60)
		})
		h.b.OnGameEnd(game("A"))
		if h.b.SummaryGoal("g1") != nil {
			t.Fatal("a profile without terms must record no goal")
		}
		h.b.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
			return goal.Profile{Name: "x", Terms: []goal.Term{durationTerm(1)}}
		})
		stale := game("A")
		stale.SessionID = "g2"
		stale.Session.DurationSeconds = fp(30)
		stale.EndReason = gamecontext.EndReasonStale
		h.b.OnGameEnd(stale)
		if h.b.SummaryGoal("g2") != nil || len(goalHistograms(t, h)) != 0 {
			t.Fatal("a stale end must record no goal")
		}
	})
}

// UC: UC-042 A12
// TestBook_SampleGoalGates: the sampler takes nothing from a game that is not
// running, and a cycle that arrives after the game ended re-creates nothing (no
// leaked game book).
func TestBook_SampleGoalGates(t *testing.T) {
	t.Run("UC-042/A12 a running game is sampled (positive control)", func(t *testing.T) {
		h := newHarness(t)
		c := withGoalHistory(game("A", "B"), h.clk.now(), 20, 0.8)
		h.b.SampleGoal(c)
		if acc := h.b.GoalAccumulator("g1"); !acc.Sampled() {
			t.Fatal("a running game must be sampled")
		}
	})
	t.Run("UC-042/A12 GameActive false takes no sample", func(t *testing.T) {
		h := newHarness(t)
		c := withGoalHistory(game("A", "B"), h.clk.now(), 20, 0.8)
		c.Session.GameActive = bp(false)
		h.b.SampleGoal(c)
		if acc := h.b.GoalAccumulator("g1"); acc.Sampled() {
			t.Fatal("a game that is not running must not be sampled")
		}
	})
	t.Run("UC-042/A12 a sample after the end re-creates nothing", func(t *testing.T) {
		for _, withGoal := range []bool{false, true} {
			h := newHarness(t)
			if withGoal {
				h.b.SetGoalResolver(func(context.Context, gamecontext.GameContext) goal.Profile {
					return goal.Profile{Name: "x", Terms: []goal.Term{durationTerm(1)}}
				})
			}
			c := withGoalHistory(game("A", "B"), h.clk.now(), 20, 0.8)
			h.b.SampleGoal(c)
			end := c
			end.EndReason = gamecontext.EndReasonZero
			h.b.OnGameEnd(end)
			h.at(30 * time.Second)
			h.b.SampleGoal(withGoalHistory(game("A", "B"), h.clk.now(), 30, 0.8))
			if acc := h.b.GoalAccumulator("g1"); acc.Sampled() {
				t.Fatalf("goal resolver %v: a late sample re-created the ended game", withGoal)
			}
		}
	})
}
