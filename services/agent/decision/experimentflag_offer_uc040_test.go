package decision

import (
	"context"
	"log/slog"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/gamecontext"
)

// experimentflag_offer_uc040_test.go covers the remote side of the experiment-flag
// skip (UC-040 A40 steps 5-6, BR-028; decide-contract §3.1, §4.5 step 2a, §7.1;
// #2251): in an experiment game the remote decide request does not offer a lever of
// the flag under test, a request left with only noop is not made, and a repeated
// experiment_flag block in the same game is quiet (as A21).

// remoteExperimentCtx is remoteContext in experiment exp_1 (control arm).
func remoteExperimentCtx() gamecontext.GameContext {
	c := remoteContext()
	c.ExperimentID = "exp_1"
	c.Arm = "control"
	return c
}

func offeredOptions(t *testing.T, be *fakeDecideBackend) []string {
	t.Helper()
	be.mu.Lock()
	defer be.mu.Unlock()
	if len(be.reqs) == 0 {
		t.Fatal("no remote request was made")
	}
	return be.reqs[0].Questions[0].Options
}

// UC: UC-040 A40 BR-028
func TestExperimentFlag_RemoteRequestOmitsLevers(t *testing.T) {
	allowed := []string{"noop", InterventionGrantShield, InterventionSetPacingProfile, InterventionAdjustGlobalSensitivity}
	cases := []struct {
		name string
		flag string
		ctx  gamecontext.GameContext
		want []string
	}{
		{"a windows experiment game is not offered set_pacing_profile", "windows", remoteExperimentCtx(),
			[]string{"noop", InterventionAdjustGlobalSensitivity, InterventionGrantShield}},
		{"a sensitivity experiment game is not offered adjust_global_sensitivity", "sensitivity", remoteExperimentCtx(),
			[]string{"noop", InterventionGrantShield, InterventionSetPacingProfile}},
		{"a game with no experiment is offered every allowed lever", "windows", remoteContext(),
			[]string{"noop", InterventionAdjustGlobalSensitivity, InterventionGrantShield, InterventionSetPacingProfile}},
		{"an experiment on another flag is offered every allowed lever", "death_grace_period_seconds", remoteExperimentCtx(),
			[]string{"noop", InterventionAdjustGlobalSensitivity, InterventionGrantShield, InterventionSetPacingProfile}},
	}
	for _, tc := range cases {
		t.Run("UC-040/A40 step 5 "+tc.name, func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
			h, _, _ := noAllowedHarness(t, allowed, be)
			h.loop.SetExperimentFlagSource(flagOf(tc.flag))
			_ = h.loop.OnEvaluate(context.Background(), tc.ctx, testTrigger())
			h.loop.AwaitInflight()
			if got := offeredOptions(t, be); !equalStrings(got, tc.want) {
				t.Fatalf("ffa.intervention options = %v, want %v", got, tc.want)
			}
		})
	}
}

// UC: UC-040 A40 A14 BR-028 BR-005
func TestExperimentFlag_OnlyLeverLeftMakesNoCall(t *testing.T) {
	t.Run("UC-040/A40 step 5 a request left with only noop is gated no_interventions_allowed", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("set_pacing_profile", 0.95, "none", 0.5, "x"))
		h, reader, _ := noAllowedHarness(t, []string{"noop", InterventionSetPacingProfile}, be)
		h.loop.SetExperimentFlagSource(flagOf("windows"))
		_ = h.loop.OnEvaluate(context.Background(), remoteExperimentCtx(), testTrigger())
		h.loop.AwaitInflight()
		if n := be.callCount(); n != 0 {
			t.Fatalf("backend calls = %d, want 0", n)
		}
		if got := gatedByReason(t, reader)[DecideGateNoInterventions]; got != 1 {
			t.Fatalf("gated[%s] = %d, want 1", DecideGateNoInterventions, got)
		}
	})
	t.Run("UC-040/A40 step 4 the same allow-list in a game with no experiment calls the backend", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		h, _, _ := noAllowedHarness(t, []string{"noop", InterventionSetPacingProfile}, be)
		h.loop.SetExperimentFlagSource(flagOf("windows"))
		_ = h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		if n := be.callCount(); n != 1 {
			t.Fatalf("backend calls = %d, want 1", n)
		}
	})
}

// UC: UC-040 A40 A21 BR-028
func TestExperimentFlag_RepeatBlockQuiet(t *testing.T) {
	t.Run("UC-040/A40 step 6 only the first experiment_flag block per lever is emitted; repeats only count", func(t *testing.T) {
		l, _, clock, decisionSpans := budgetLoop(t, 100, pacing("calm"))
		l.SetExperimentFlagSource(flagOf("windows"))
		reader := metric.NewManualReader()
		l.decisions = newDecisionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		logs := &syncBuffer{}
		l.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
		timeline := 0
		l.interventionRecorder = func(string, string, bool) { timeline++ }

		const blocks = 4
		for i := 0; i < blocks; i++ {
			expectOutcome(t, runCycle(l, experimentCtx("control")), "set_pacing_profile/:experiment_flag")
			*clock = clock.Add(2 * time.Second)
		}
		if n := decisionSpans(); n != 1 {
			t.Fatalf("agent.decision spans = %d, want 1 (the first block only)", n)
		}
		if n := logs.count("agent.decision_blocked"); n != 1 {
			t.Errorf("agent.decision_blocked warnings = %d, want 1", n)
		}
		if n := logs.count("level=DEBUG msg=agent.decision_experiment_flag_repeat"); n != blocks-1 {
			t.Errorf("repeat debug lines = %d, want %d", n, blocks-1)
		}
		if timeline != 1 {
			t.Errorf("timeline entries = %d, want 1", timeline)
		}
		if got := decisionsByLabels(t, reader)["set_pacing_profile/true/experiment_flag"]; got != blocks {
			t.Errorf("agent_decisions_total{set_pacing_profile,true,experiment_flag} = %d, want %d", got, blocks)
		}
	})
	t.Run("UC-040/A40 step 6 a different lever reports its own first block", func(t *testing.T) {
		l, _, _, decisionSpans := budgetLoop(t, 100, pacing("calm"), globalSens("1"))
		flag := "windows"
		l.SetExperimentFlagSource(func(gamecontext.GameContext) string { return flag })
		_ = runCycle(l, experimentCtx("control"))
		_ = runCycle(l, experimentCtx("control"))
		flag = "sensitivity"
		_ = runCycle(l, experimentCtx("control"))
		// windows: pacing blocked (span), sensitivity dispatched (span); repeat:
		// pacing quiet, sensitivity dispatched again (span, already-applied is off);
		// sensitivity experiment: pacing dispatched (span), sensitivity blocked first time (span).
		if n := decisionSpans(); n != 5 {
			t.Fatalf("agent.decision spans = %d, want 5", n)
		}
	})
}
