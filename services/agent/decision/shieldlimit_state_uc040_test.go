package decision

import (
	"context"
	"math/rand/v2"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// The decide state's shield_limited marker and applied levels, fed from the loop's
// ledgers (#2066; UC-040 main step 4, A33, A35; decide-contract §1.2).

func playerIn(t *testing.T, st decide.State, serial string) decide.PlayerState {
	t.Helper()
	for _, p := range st.Players {
		if p.Serial == serial {
			return p
		}
	}
	t.Fatalf("player %s missing from the state", serial)
	return decide.PlayerState{}
}

// UC: UC-040 main step 4 A33 BR-022
func TestShieldLimitedState_RemoteRequest(t *testing.T) {
	t.Run("UC-040/A33 a capped player is shield_limited in the remote request, not shield_charged", func(t *testing.T) {
		l, _, _, _ := limitLoop(t, 100, 1, 0, budgetShield("WEAK"))
		if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("first grant outcomes = %v, want dispatched", got)
		}
		st := l.buildDecideRequest(budgetCtx(), allInterventions(), false).State
		if w := playerIn(t, st, "WEAK"); !w.ShieldLimited || w.ShieldCharged {
			t.Fatalf("WEAK limited=%v charged=%v, want limited only", w.ShieldLimited, w.ShieldCharged)
		}
		if playerIn(t, st, "S").ShieldLimited {
			t.Fatal("S has no shield: must not be shield_limited")
		}
	})
	t.Run("UC-040/A33 a cooling-down player is shield_limited", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 100, 3, 15, budgetShield("WEAK"))
		_ = runCycle(l, budgetCtx())
		*clock = clock.Add(10 * time.Second) // within 5 s shield + 2 s + 15 s cooldown
		if !playerIn(t, l.buildDecideRequest(budgetCtx(), allInterventions(), false).State, "WEAK").ShieldLimited {
			t.Fatal("WEAK in the cooldown must be shield_limited")
		}
	})
}

// UC: UC-040 main step 4 A35 BR-023
func TestAppliedLevelState_RemoteRequest(t *testing.T) {
	t.Run("UC-040/A35 a written sensitivity factor is in the remote request's state", func(t *testing.T) {
		l, _, _, _ := appliedLoop(t, sens("WEAK", "0.8"))
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
		st := l.buildDecideRequest(budgetCtx(), allInterventions(), false).State
		w := playerIn(t, st, "WEAK")
		if w.SensitivityFactor == nil || *w.SensitivityFactor != 0.8 {
			t.Fatalf("WEAK sensitivity_factor = %v, want 0.8", w.SensitivityFactor)
		}
		if playerIn(t, st, "S").SensitivityFactor != nil {
			t.Fatal("S has no applied factor")
		}
	})
	t.Run("UC-040/A35 a rules write with an empty value shows the writer's default 1.5", func(t *testing.T) {
		l, _, _, _ := appliedLoop(t, sens("WEAK", ""))
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
		w := playerIn(t, l.buildDecideRequest(budgetCtx(), allInterventions(), false).State, "WEAK")
		if w.SensitivityFactor == nil || *w.SensitivityFactor != 1.5 {
			t.Fatalf("WEAK sensitivity_factor = %v, want the writer default 1.5", w.SensitivityFactor)
		}
	})
	t.Run("UC-040/A35 without suppression the state carries no factor", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 100, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		st := l.buildDecideRequest(budgetCtx(), allInterventions(), false).State
		if playerIn(t, st, "WEAK").SensitivityFactor != nil {
			t.Fatal("a loop without suppression must not annotate a factor")
		}
	})
}

// UC: UC-040 main step 4 A33
func TestShieldLimitedState_RulesPathUsesRankingSnapshot(t *testing.T) {
	t.Run("UC-040/A33 the rules-path state marks the capped player the ranking skipped", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Objectives = balancedFocused()
		snap.Policy = flags.Policy{MaxInterventionsPerMinute: 50, MaxShieldsPerPlayerPerGame: 1}
		l, _ := decideRecordingLoop(t, snap)
		clock := time.Unix(50_000, 0)
		l.now = func() time.Time { return clock }
		l.Actions = &capturingSink{}
		rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
		rules.SetShieldSource(l.ShieldSerials)
		l.Rules = rules
		var st LayerState
		l.runDecision(context.Background(), snap, chargeRulesCtx(), budgetShield("WEAK"), clock, &st)
		if got := outcomeSummary(st); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("grant outcomes = %v, want dispatched", got)
		}
		clock = clock.Add(time.Minute)
		_, req, _, ranked := rules.EvaluateDecide(context.Background(), chargeRulesCtx(), l.rulesRequestBuilder(chargeRulesCtx(), allInterventions()))
		if !ranked {
			t.Fatal("EvaluateDecide was throttled")
		}
		if !playerIn(t, req.State, "WEAK").ShieldLimited {
			t.Fatal("the rules-path state must mark the capped WEAK shield_limited")
		}
	})
}

// UC: UC-040 main step 4 A33
func TestRulesBackend_ShieldLimitedStateIsNoShield(t *testing.T) {
	t.Run("UC-040/A33 RulesBackend.Decide gives no grant_shield to a shield_limited player of the request state", func(t *testing.T) {
		newBackend := func() *RulesBackend {
			clock := time.Unix(10_000, 0)
			return NewRulesBackend(newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
				func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2))))
		}
		c := chargeRulesCtx()
		allowed := []string{InterventionGrantShield}
		pick := func(req decide.Request) string {
			resp, err := newBackend().Decide(context.Background(), req)
			if err != nil {
				t.Fatalf("Decide: %v", err)
			}
			iv, _ := resp.Find(decide.QuestionFFAIntervention)
			tg, _ := resp.Find(decide.QuestionFFATarget)
			return iv.Choice + "/" + tg.Choice
		}
		if got := pick(ffaRequest(c, allowed)); got != "grant_shield/WEAK" {
			t.Fatalf("precondition: Decide picks %s, want grant_shield/WEAK", got)
		}
		st := decide.BuildStateWith(c, decide.Annotations{NoShield: map[string]bool{"WEAK": true}})
		if !playerIn(t, st, "WEAK").ShieldLimited {
			t.Fatal("precondition: WEAK must be shield_limited in the state")
		}
		req := decide.Request{State: st, Questions: decide.FFAQuestions(st, allowed), Context: &c}
		if got := pick(req); got == "grant_shield/WEAK" {
			t.Fatal("a shield_limited WEAK must not get grant_shield (treated like shield_charged)")
		}
	})
}
