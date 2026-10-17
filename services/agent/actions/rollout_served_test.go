package actions

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
	"github.com/joustmania/agent/infracontext"
)

// rolloutFlagService is the fake flag service loaded with the shipped rollout set
// and its registry policies.
func rolloutFlagService(t *testing.T) *fake.Service {
	t.Helper()
	policies, err := fake.RegistryPolicies(filepath.Join("..", "..", "flag-service", "registry.yaml"), FlagSetRollout)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	svc := fake.New()
	if err := svc.LoadSetWithPolicies(FlagSetRollout, filepath.Join("..", "..", "flagd", "rollout.json"), policies); err != nil {
		t.Fatalf("load rollout.json: %v", err)
	}
	return svc
}

func servedVariant(t *testing.T, svc *fake.Service) string {
	t.Helper()
	view, err := svc.GetFlag(context.Background(), FlagSetRollout, flagCurrentControllerCount)
	if err != nil {
		t.Fatalf("get flag: %v", err)
	}
	return view.EffectiveDefault
}

// ServedStage reports the served stage, whether it is the agent's own overlay and
// the boot epoch, so the loop can tell its write from an operator's and notice a
// flag-service restart.
// UC: UC-050 BR-001
func TestRolloutWriter_ServedStage(t *testing.T) {
	svc := rolloutFlagService(t)
	w := NewRolloutWriter(svc, discardLogger())
	ctx := context.Background()

	t.Run("UC-050/BR-001 baseline is not the agent's", func(t *testing.T) {
		got, err := w.ServedStage()
		if err != nil || got.ByAgent || !got.OnLadder || got.Value != StageNoneValue || got.Epoch == "" {
			t.Fatalf("ServedStage() = (%+v, %v), want baseline none, not by agent, with an epoch", got, err)
		}
	})
	t.Run("UC-050/BR-001 the agent's write", func(t *testing.T) {
		if err := w.SetControllerCount(StageThreeVariant); err != nil {
			t.Fatal(err)
		}
		got, err := w.ServedStage()
		if err != nil || !got.ByAgent || got.Value != StageThreeValue {
			t.Fatalf("ServedStage() = (%+v, %v), want three by agent", got, err)
		}
	})
	t.Run("UC-050/BR-001 an operator's reset", func(t *testing.T) {
		if _, err := svc.Put(ctx, FlagSetRollout, flagCurrentControllerCount,
			flagservice.Write{Variant: StageNoneVariant, Writer: "stage"}); err != nil {
			t.Fatal(err)
		}
		got, err := w.ServedStage()
		if err != nil || got.ByAgent || got.Value != StageNoneValue {
			t.Fatalf("ServedStage() = (%+v, %v), want none, not by agent", got, err)
		}
	})
	t.Run("UC-051/A2 a flag-service restart changes the epoch", func(t *testing.T) {
		before, _ := w.ServedStage()
		svc.Restart()
		got, err := w.ServedStage()
		if err != nil || got.Epoch == before.Epoch || got.ByAgent {
			t.Fatalf("ServedStage() = (%+v, %v), want a new epoch and no agent overlay (before %q)", got, err, before.Epoch)
		}
	})
}

// A FlagService that cannot read flags makes ServedStage an error, so the loop
// keeps its state (fail safe).
// UC: UC-050 BR-001
func TestRolloutWriter_ServedStageWithoutReader(t *testing.T) {
	w, _ := newTestRolloutWriter(t)
	if _, err := w.ServedStage(); err == nil {
		t.Fatal("ServedStage() on a write-only service: want an error")
	}
}

// End to end with the real gated actuator, the shipped rollout set and the
// recording fake flag service: after a real expansion the stage is reset behind
// the agent's back (an operator write, or a flag-service restart). The observed
// stage drops to none and the loop climbs again, without an agent restart.
// UC: UC-050 BR-001
// UC: UC-051 A2
// UC: UC-070 A1
func TestGatedRolloutActuator_LoopRecoversFromExternalReset(t *testing.T) {
	cases := []struct {
		name  string
		reset func(t *testing.T, svc *fake.Service)
	}{
		{"UC-070/A1 operator resets current_controller_count", func(t *testing.T, svc *fake.Service) {
			if _, err := svc.Put(context.Background(), FlagSetRollout, flagCurrentControllerCount,
				flagservice.Write{Variant: StageNoneVariant, Writer: "stage"}); err != nil {
				t.Fatal(err)
			}
		}},
		{"UC-051/A2 flag service restarts", func(t *testing.T, svc *fake.Service) { svc.Restart() }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc := rolloutFlagService(t)
			act := NewGatedRolloutActuator(context.Background(), staticGate(true), NewRolloutWriter(svc, discardLogger()), nil)
			loop := decision.NewInfraLoop(discardLogger(), time.Hour, nil, act)
			loop.SetGate(func(infracontext.InfraContext, time.Time) bool { return true })
			snap := func(stage int) infracontext.InfraContext {
				hz := 30.0
				gap, dropped := 20.0, 0.01
				return infracontext.InfraContext{Window: infracontext.WindowSignals{
					EventGapMs: &gap, DroppedEventsPct: &dropped, MovementUpdateHz: &hz,
					TargetBackend: "unstable", RolloutCount: stage,
				}}
			}

			loop.OnInfraEvaluate(context.Background(), snap(StageNoneValue))
			if got := servedVariant(t, svc); got != StageOneVariant {
				t.Fatalf("first expansion: served %q, want %q", got, StageOneVariant)
			}
			tc.reset(t, svc)
			if got := servedVariant(t, svc); got != StageNoneVariant {
				t.Fatalf("after reset: served %q, want %q", got, StageNoneVariant)
			}
			// One cycle: the reset voided the last write and its expansion time, so the
			// loop expands at once even with an hour-long dwell.
			loop.OnInfraEvaluate(context.Background(), snap(StageNoneValue))
			if got := servedVariant(t, svc); got != StageOneVariant {
				t.Fatalf("after the reset the loop did not expand again: served %q, want %q", got, StageOneVariant)
			}
		})
	}
}
