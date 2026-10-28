package decision

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// experimentflag_uc040_test.go covers the experiment-flag skip (UC-040 A40,
// BR-028; decide-contract §4.5 step 2a; #1975): in a game of an experiment, either
// arm, a lever of the flag under test is blocked experiment_flag, with no write and
// no budget, and does not end rank-order admission; a lever of another flag, a game
// with no experiment and a loop without the source decide as before.

func pacing(value string) Decision {
	return Decision{Intervention: InterventionSetPacingProfile, Value: value,
		ObjectiveServed: ObjectiveBalanced, Objectives: balancedFocused()}
}

func globalSens(value string) Decision {
	return Decision{Intervention: InterventionAdjustGlobalSensitivity, Value: value,
		ObjectiveServed: ObjectiveBalanced, Objectives: balancedFocused()}
}

// experimentCtx is budgetCtx in a shadow game of experiment exp_1 in arm.
func experimentCtx(arm string) gamecontext.GameContext {
	c := budgetCtx()
	c.GameKind = "shadow"
	c.ExperimentID = "exp_1"
	c.Arm = arm
	return c
}

// flagOf answers flagKey for exp_1 and "" for any other game.
func flagOf(flagKey string) func(c gamecontext.GameContext) string {
	return func(c gamecontext.GameContext) string {
		if c.ExperimentID == "exp_1" {
			return flagKey
		}
		return ""
	}
}

func writesOfIntervention(sink *capturingSink, intervention string) int {
	n := 0
	for _, d := range sink.decisions() {
		if d.Intervention == intervention {
			n++
		}
	}
	return n
}

// UC: UC-040 A40 BR-028
func TestExperimentFlag_LeverBlockedInBothArms(t *testing.T) {
	for _, arm := range []string{"experimental", "control"} {
		t.Run("UC-040/A40 BR-028 set_pacing_profile in the "+arm+" arm of a windows experiment is blocked experiment_flag, no write, no budget", func(t *testing.T) {
			l, sink, _, _ := budgetLoop(t, 100, pacing("calm"))
			l.SetExperimentFlagSource(flagOf("windows"))
			before := limiterEntries(l)
			expectOutcome(t, runCycle(l, experimentCtx(arm)), "set_pacing_profile/:experiment_flag")
			if n := writesOfIntervention(sink, InterventionSetPacingProfile); n != 0 {
				t.Fatalf("pacing writes = %d, want 0", n)
			}
			if after := limiterEntries(l); after != before {
				t.Fatalf("limiter entries %d -> %d: experiment_flag must not charge the budget", before, after)
			}
		})
	}
	t.Run("UC-040/A40 BR-028 adjust_global_sensitivity in a sensitivity experiment is blocked experiment_flag", func(t *testing.T) {
		l, sink, _, _ := budgetLoop(t, 100, globalSens("1"))
		l.SetExperimentFlagSource(flagOf("sensitivity"))
		expectOutcome(t, runCycle(l, experimentCtx("control")), "adjust_global_sensitivity/:experiment_flag")
		if n := writesOfIntervention(sink, InterventionAdjustGlobalSensitivity); n != 0 {
			t.Fatalf("sensitivity writes = %d, want 0", n)
		}
	})
	t.Run("UC-040/A40 BR-014 experiment_flag does not end rank-order admission", func(t *testing.T) {
		// Budget 1 shield: the blocked lever ranked first must not hold the cycle.
		l, sink, _, _ := budgetLoop(t, int(interventionCost(InterventionGrantShield)), pacing("calm"), budgetShield("S"))
		l.SetExperimentFlagSource(flagOf("windows"))
		got := outcomeSummary(runCycle(l, experimentCtx("control")))
		if !equalStrings(got, []string{"set_pacing_profile/:experiment_flag", "grant_shield/S:dispatched"}) {
			t.Fatalf("outcomes = %v, want the lever blocked and the shield dispatched", got)
		}
		if n := writesOfIntervention(sink, InterventionGrantShield); n != 1 {
			t.Fatalf("shield writes = %d, want 1", n)
		}
	})
}

// UC: UC-040 A40 BR-028
func TestExperimentFlag_OtherCasesDecideAsBefore(t *testing.T) {
	t.Run("UC-040/A40 step 4 a lever of another flag is dispatched in an experiment game", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 100, pacing("calm"))
		l.SetExperimentFlagSource(flagOf("death_grace_period_seconds"))
		expectOutcome(t, runCycle(l, experimentCtx("control")), "set_pacing_profile/:dispatched")
	})
	t.Run("UC-040/A40 step 4 a game with no experiment is dispatched", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 100, pacing("calm"))
		l.SetExperimentFlagSource(flagOf("windows"))
		expectOutcome(t, runCycle(l, budgetCtx()), "set_pacing_profile/:dispatched")
	})
	t.Run("UC-040/A40 a loop without the source skips nothing", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 100, pacing("calm"))
		expectOutcome(t, runCycle(l, experimentCtx("control")), "set_pacing_profile/:dispatched")
	})
	t.Run("UC-040/A40 step 1 not_allowed keeps precedence over experiment_flag", func(t *testing.T) {
		l, _, _, _ := budgetLoop(t, 100, pacing("calm"))
		snap := l.Flags.Evaluate(t.Context())
		snap.InterventionsAllowed = []string{InterventionGrantShield}
		l.Flags = fakeFlags{snap: snap}
		l.SetExperimentFlagSource(flagOf("windows"))
		expectOutcome(t, runCycle(l, experimentCtx("control")), "set_pacing_profile/:not_allowed")
	})
}

// UC: UC-040 BR-028
func TestExperimentLevers_Map(t *testing.T) {
	t.Run("UC-040/BR-028 the levers replace windows and sensitivity; factors are not levers", func(t *testing.T) {
		got := ExperimentLevers()
		want := map[string]string{
			InterventionSetPacingProfile:        "windows",
			InterventionAdjustGlobalSensitivity: "sensitivity",
		}
		if len(got) != len(want) {
			t.Fatalf("levers = %v, want %v", got, want)
		}
		for k, v := range want {
			if got[k] != v {
				t.Fatalf("levers[%s] = %q, want %q", k, got[k], v)
			}
		}
	})
}
