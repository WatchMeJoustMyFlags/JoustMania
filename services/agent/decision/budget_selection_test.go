package decision

import (
	"context"
	"fmt"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// budget_selection_test.go covers the budget-aware admission of #1326: a scarce
// per-game budget goes to the highest-ranked decision (UC-040 A20, BR-014),
// nudges keep a reserve for protective interventions (BR-015), and a refusal
// the budget already reported is not re-emitted as a span (A21).

// balancedFocused is the shipped objectives default (services/flagd/agent.json).
func balancedFocused() map[string]float64 {
	return map[string]float64{
		ObjectiveEndurance: 0.1, ObjectiveBalanced: 0.7, ObjectiveAccelerate: 0.1, ObjectiveChaos: 0.1,
	}
}

func budgetShield(target string) Decision {
	return Decision{Intervention: InterventionGrantShield, TargetSerial: target, ObjectiveServed: ObjectiveBalanced, Objectives: balancedFocused()}
}

func budgetNudge(target string) Decision {
	return Decision{Intervention: InterventionSendControllerEffect, TargetSerial: target, ObjectiveServed: ObjectiveChaos, Objectives: balancedFocused()}
}

// budgetLoop is a loop with a fixed decision list, a recording tracer and a
// controllable clock, at the given per-minute budget.
func budgetLoop(t *testing.T, budget int, out ...Decision) (*Loop, *capturingSink, *time.Time, func() int) {
	t.Helper()
	snap := rulesSnapshot()
	snap.Policy.MaxInterventionsPerMinute = budget
	snap.Objectives = balancedFocused()
	l, sr := decideRecordingLoop(t, snap)
	l.Rules = fakeRules{out: out}
	sink := &capturingSink{}
	l.Actions = sink
	clock := time.Unix(50_000, 0)
	l.now = func() time.Time { return clock }
	decisionSpans := func() int { return len(spansByName(sr.Ended(), SpanDecision)) }
	return l, sink, &clock, decisionSpans
}

func budgetCtx() gamecontext.GameContext {
	return gameCtx(40, nil, map[string]testPlayer{"WEAK": {active: true}, "S": {active: true}})
}

func outcomeSummary(st LayerState) []string {
	out := make([]string, 0, len(st.Outcomes))
	for _, o := range st.Outcomes {
		v := "dispatched"
		if !o.Dispatched {
			v = string(o.BlockReason)
		}
		out = append(out, fmt.Sprintf("%s/%s:%s", o.Intervention, o.TargetSerial, v))
	}
	return out
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// UC: UC-040 A20 BR-014
func TestScarceBudgetGoesToTheTopDecision(t *testing.T) {
	t.Run("UC-040/A20 a cheaper lower-ranked nudge does not take the half slot the top shield waits for", func(t *testing.T) {
		l, sink, clock, _ := budgetLoop(t, 2, budgetShield("WEAK"), budgetNudge("S"))
		// 1.5 of 2 already spent 30 s ago: 0.5 free, the shield (1.0) does not fit.
		l.limiter.allow(clock.Add(-30*time.Second), 1.5, 2)

		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"grant_shield/WEAK:rate_limit", "send_controller_effect/S:rate_limit"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v (the nudge must not spend the budget ahead of the shield)", got, want)
		}
		if n := len(sink.decisions()); n != 0 {
			t.Fatalf("applied %d decisions, want none", n)
		}

		// The old 1.5 leaves the window: the shield is admitted first.
		*clock = clock.Add(31 * time.Second)
		st = l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		if len(st.Outcomes) == 0 || !st.Outcomes[0].Dispatched || st.Outcomes[0].Intervention != InterventionGrantShield {
			t.Fatalf("outcomes = %v, want grant_shield dispatched first", outcomeSummary(st))
		}
	})

	t.Run("UC-040/A20 a decision that exceeds the whole budget does not hold the cycle", func(t *testing.T) {
		end := Decision{Intervention: InterventionEndGame, ObjectiveServed: ObjectiveAccelerate}
		l, _, _, _ := budgetLoop(t, 1, end, budgetShield("WEAK"))
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"end_game/:rate_limit", "grant_shield/WEAK:dispatched"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v (end_game costs 2 > budget 1 and can never fit)", got, want)
		}
	})

	t.Run("UC-040/A20 a not-allowed top decision does not hold the cycle", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 2, Decision{Intervention: "not_in_vocabulary"}, budgetShield("WEAK"))
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"not_in_vocabulary/:not_allowed", "grant_shield/WEAK:dispatched"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v", got, want)
		}
	})
}

// UC: UC-040 A20 BR-014
func TestAmpleBudgetAdmitsEveryDecisionInRankOrder(t *testing.T) {
	t.Run("UC-040/A20 ample budget: every decision dispatches as before", func(t *testing.T) {
		tempo := Decision{Intervention: InterventionAdjustMusicTempo, ObjectiveServed: ObjectiveEndurance}
		l, sink, _, decisionSpans := budgetLoop(t, 100, tempo, budgetShield("WEAK"), budgetNudge("S"))
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"adjust_music_tempo/:dispatched", "grant_shield/WEAK:dispatched", "send_controller_effect/S:dispatched"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v", got, want)
		}
		if n := len(sink.decisions()); n != 3 {
			t.Fatalf("applied %d, want 3", n)
		}
		if n := decisionSpans(); n != 3 {
			t.Fatalf("agent.decision spans = %d, want 3", n)
		}
	})

	t.Run("UC-040/A20 the budget exactly covers the cycle: nothing is held", func(t *testing.T) {
		tempo := Decision{Intervention: InterventionAdjustMusicTempo, ObjectiveServed: ObjectiveEndurance}
		l, _, _, _ := budgetLoop(t, 2, tempo, budgetShield("WEAK"))
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"adjust_music_tempo/:dispatched", "grant_shield/WEAK:dispatched"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v", got, want)
		}
	})
}

// UC: UC-040 A20 BR-015
func TestShieldBeatsStatueNudge(t *testing.T) {
	t.Run("UC-040/A20 nudges leave one medium intervention free", func(t *testing.T) {
		l, sink, clock, _ := budgetLoop(t, 2, budgetNudge("S1"), budgetNudge("S2"), budgetNudge("S3"))
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want := []string{"send_controller_effect/S1:dispatched", "send_controller_effect/S2:dispatched", "send_controller_effect/S3:rate_limit"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v (nudges may spend 1.0 of 2)", got, want)
		}
		// The reserve is still there for a shield.
		l.Rules = fakeRules{out: []Decision{budgetNudge("S4"), budgetShield("WEAK")}}
		*clock = clock.Add(time.Second)
		st = l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		want = []string{"send_controller_effect/S4:rate_limit", "grant_shield/WEAK:dispatched"}
		if got := outcomeSummary(st); !equalStrings(got, want) {
			t.Fatalf("outcomes = %v, want %v (a reserve-refused nudge does not hold the shield)", got, want)
		}
		if n := len(sink.decisions()); n != 3 {
			t.Fatalf("applied %d, want 3 (two nudges + the shield)", n)
		}
	})

	t.Run("UC-040/A20 chaos dominant: nudges get no reserve", func(t *testing.T) {
		chaos := map[string]float64{ObjectiveChaos: 0.7, ObjectiveBalanced: 0.1}
		n1, n2, n3, n4 := budgetNudge("S1"), budgetNudge("S2"), budgetNudge("S3"), budgetNudge("S4")
		for _, d := range []*Decision{&n1, &n2, &n3, &n4} {
			d.Objectives = chaos
		}
		l, _, _, _ := budgetLoop(t, 2, n1, n2, n3, n4)
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		if st.Dispatched != 4 {
			t.Fatalf("outcomes = %v, want all four nudges dispatched under chaos_focused", outcomeSummary(st))
		}
	})

	t.Run("UC-040/A20 nudgeReserve follows the weights", func(t *testing.T) {
		cases := []struct {
			name string
			d    Decision
			flag map[string]float64
			want float64
		}{
			{"shield never reserves", budgetShield("W"), nil, 0},
			{"nudge under balanced_focused", budgetNudge("S"), nil, protectiveReserve},
			{"nudge, flag weights when the decision has none", Decision{Intervention: InterventionSendControllerEffect}, map[string]float64{ObjectiveChaos: 1}, 0},
			{"nudge, engine default weights", Decision{Intervention: InterventionSendControllerEffect}, nil, protectiveReserve},
			{"nudge, chaos tie is not dominance", Decision{Intervention: InterventionSendControllerEffect, Objectives: map[string]float64{ObjectiveChaos: 0.5, ObjectiveBalanced: 0.5}}, nil, protectiveReserve},
		}
		for _, tc := range cases {
			if got := nudgeReserve(tc.d, tc.flag); got != tc.want {
				t.Errorf("%s: nudgeReserve = %v, want %v", tc.name, got, tc.want)
			}
		}
	})

	t.Run("UC-040/A20 dry run c8e9aa27: six statues, balanced_focused, budget 2", func(t *testing.T) {
		// The first 30 s only the six held-still players fire R8; then a player is
		// eliminated and R4 wants to shield the weakest. Before #1326 the nudges
		// had filled the budget and the shield was refused (0 of 49 in the dry run).
		cfg := withWeights(balancedFocused())
		clock := time.Unix(20_000, 0)
		snap := rulesSnapshot()
		snap.Policy.MaxInterventionsPerMinute = 2
		snap.Objectives = balancedFocused()
		l, _ := decideRecordingLoop(t, snap)
		l.Rules = newTestEngine(cfg, &clock)
		l.now = func() time.Time { return clock }
		sink := &capturingSink{}
		l.Actions = sink

		ctxAt := func(dur float64) gamecontext.GameContext {
			players := map[string]testPlayer{}
			for i := 1; i <= 6; i++ {
				p := testPlayer{variance: fptr(0), active: true}
				if dur >= 30 {
					p.skill = fptr(0.5)
					if i == 1 {
						p.skill = fptr(0.1)
					}
				}
				players[fmt.Sprintf("MOCK000%d", i)] = p
			}
			var elims []string
			if dur >= 30 {
				elims = []string{"MOCK0007"}
			}
			return gameCtx(dur, elims, players)
		}

		firstShield := -1.0
		for dur := 0.0; dur <= 40; dur++ {
			st := l.OnEvaluate(context.Background(), ctxAt(dur), testTrigger())
			for _, o := range st.Outcomes {
				if o.Intervention == InterventionGrantShield && firstShield < 0 {
					if !o.Dispatched {
						t.Fatalf("t=%.0fs: first grant_shield refused with %s; outcomes %v", dur, o.BlockReason, outcomeSummary(st))
					}
					firstShield = dur
				}
			}
			advance(&clock, time.Second)
		}
		if firstShield != 30 {
			t.Fatalf("first grant_shield at t=%.0f, want 30 (its first try)", firstShield)
		}
		nudges := 0
		for _, d := range sink.decisions() {
			if d.Intervention == InterventionSendControllerEffect {
				nudges++
			}
		}
		if nudges != 2 {
			t.Fatalf("applied %d nudges in the first window, want 2 (1.0 of the 2 budget)", nudges)
		}
	})
}

// UC: UC-040 A21 BR-014
func TestRateLimitedRepeatIsNotReEmitted(t *testing.T) {
	t.Run("UC-040/A21 a refusal repeating while the budget is unchanged emits no new span", func(t *testing.T) {
		l, sink, clock, decisionSpans := budgetLoop(t, 2, budgetShield("WEAK"))
		l.limiter.allow(*clock, 2, 2) // budget full now

		for i := 0; i < 10; i++ {
			st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
			want := []string{"grant_shield/WEAK:rate_limit"}
			if got := outcomeSummary(st); !equalStrings(got, want) {
				t.Fatalf("cycle %d: outcomes = %v, want %v (still recorded in the layer state)", i, got, want)
			}
			*clock = clock.Add(5 * time.Second)
		}
		if n := decisionSpans(); n != 1 {
			t.Fatalf("agent.decision spans = %d after 10 refused cycles, want 1", n)
		}

		// The full budget leaves the window: the shield is admitted (and spanned).
		*clock = clock.Add(15 * time.Second)
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		if st.Dispatched != 1 {
			t.Fatalf("outcomes = %v, want the shield dispatched once the budget frees", outcomeSummary(st))
		}
		if n := decisionSpans(); n != 2 {
			t.Fatalf("agent.decision spans = %d, want 2", n)
		}
		if n := len(sink.decisions()); n != 1 {
			t.Fatalf("applied %d, want 1", n)
		}
	})

	t.Run("UC-040/A21 a changed budget reports the refusal in full again", func(t *testing.T) {
		l, _, clock, decisionSpans := budgetLoop(t, 2, budgetShield("WEAK"))
		l.limiter.allow(clock.Add(-50*time.Second), 0.5, 2)
		l.limiter.allow(*clock, 1.5, 2) // full; 0.5 frees in 10 s, still too little for the shield

		_ = l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		*clock = clock.Add(time.Second)
		_ = l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		if n := decisionSpans(); n != 1 {
			t.Fatalf("spans = %d before the budget changes, want 1", n)
		}
		*clock = clock.Add(10 * time.Second) // the 0.5 leaves: the budget state changed
		st := l.OnEvaluate(context.Background(), budgetCtx(), testTrigger())
		if st.Dispatched != 0 {
			t.Fatalf("outcomes = %v, want still refused (0.5 free)", outcomeSummary(st))
		}
		if n := decisionSpans(); n != 2 {
			t.Fatalf("spans = %d after the budget changed, want 2", n)
		}
	})

	t.Run("UC-040/A21 the decide span still reports the repeat as blocked", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Policy.MaxInterventionsPerMinute = 2
		l, sr := decideRecordingLoop(t, snap)
		clock := time.Unix(30_000, 0)
		cfg := withWeights(map[string]float64{ObjectiveBalanced: 1})
		l.Rules = newTestEngine(cfg, &clock)
		l.now = func() time.Time { return clock }
		l.Actions = &capturingSink{}
		c := gameCtx(30, []string{"GONE"}, map[string]testPlayer{
			"WEAK": {skill: fptr(0.2), active: true}, "MID": {skill: fptr(0.5), active: true},
		})
		l.limiter.allow(clock, 2, 2)
		for i := 0; i < 3; i++ {
			_ = l.OnEvaluate(context.Background(), c, testTrigger())
			advance(&clock, 2*time.Second)
		}
		decides := spansByName(sr.Ended(), SpanDecide)
		if len(decides) != 3 {
			t.Fatalf("agent.decide spans = %d, want one per cycle (3)", len(decides))
		}
		for i, s := range decides {
			if v, ok := attrValue(s, AttrDecideBlockReason); !ok || v.AsString() != string(ReasonRateLimit) {
				t.Fatalf("agent.decide #%d block_reason = %v (present %v), want rate_limit", i, v.AsString(), ok)
			}
		}
		if n := len(spansByName(sr.Ended(), SpanDecision)); n != 1 {
			t.Fatalf("agent.decision spans = %d, want 1", n)
		}
	})
}
