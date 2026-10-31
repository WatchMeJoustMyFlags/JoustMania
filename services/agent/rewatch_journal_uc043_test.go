package main

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/promote"
)

// rewatch_journal_uc043_test.go covers the experiment loop's half of journaling a
// re-watch revert (UC-043 step 7, #1883): buildExperimentLoop wires the Promoter's
// re-watch journal to the registry, so the revert lands in the experiment journal.

// insufficientWatcher always has too few real games, so every promotion re-watches.
type insufficientWatcher struct{}

func (insufficientWatcher) Degraded(context.Context, string, float64, time.Time) (bool, error) {
	return false, nil
}

func (insufficientWatcher) Observe(context.Context, string, float64, time.Time) (promote.WatchVerdict, error) {
	return promote.VerdictInsufficient, nil
}

// heldRewatcher keeps the re-watch revert for the test to fire.
type heldRewatcher struct {
	revert func(context.Context)
}

func (h *heldRewatcher) Schedule(_ context.Context, _ string, _ float64, _ time.Time, _ promote.Watcher, revert func(context.Context)) {
	h.revert = revert
}

// switchableReverter fails while err is set.
type switchableReverter struct {
	calls int
	err   error
}

func (r *switchableReverter) RevertRealDefault(context.Context, string) error {
	r.calls++
	return r.err
}

// UC: UC-043 main
// UC: UC-043 A9
func TestUC043_Step7_RewatchRevertJournaledByLoop(t *testing.T) {
	t.Run("UC-043/main step 7 a failed re-watch revert lands in the journal, the kill switch journals the revert", func(t *testing.T) {
		seeded, _, lc := loopForConfigTest(t, 8)
		root := t.TempDir()
		seeded.registry = experiment.NewRegistry(experiment.RegistryConfig{
			Root: root, MaxShadowGames: 8, Spawner: &recordingSpawner{},
			Verdict: experiment.NewVerdict(3, 0.5, nil), Log: testLogger(),
		})
		lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
		seeded.allocateIfEnabled(context.Background())
		live := seeded.registry.Live()
		if len(live) != 1 {
			t.Fatalf("precondition: %d live experiments, want 1", len(live))
		}

		rev := &switchableReverter{err: errRewatchFlagService}
		rw := &heldRewatcher{}
		promoter := promote.NewPromoter(&recordingGitHub{}, nil, &recordingRealDefault{}, insufficientWatcher{}, rev, promote.RepoRef{}, nil, testLogger())
		promoter.SetRewatcher(rw)
		loop := buildExperimentLoop(nil, promoter, nil, gameServiceForDeclarer(t), nil, testLogger())
		loop.registry = seeded.registry // the journal is read at call time

		proposal := experiment.Proposal{FlagKey: "nonstop.respawn_seconds", ExperimentalValue: 6.0, ExperimentID: live[0]}
		cfg := promote.Config{Mode: promote.ModeAutonomous, Target: promote.TargetLocal, Enabled: true}
		ev := promote.Evidence{FitnessBefore: 0.5, RealDefaultValue: 6.0}
		if res := promoter.Promote(context.Background(), proposal, experiment.Decision{Outcome: experiment.OutcomePromote}, cfg, ev); res.Outcome != promote.OutcomeApplied {
			t.Fatalf("precondition: outcome = %q (%s), want applied", res.Outcome, res.Reason)
		}
		if rw.revert == nil {
			t.Fatal("precondition: no re-watch scheduled")
		}

		// The re-watch tries the revert once; the kill switch finishes it (A1).
		rw.revert(context.Background())
		rev.err = nil
		loop.revertPromotionsOnKillSwitch(context.Background())

		events := promotionEvents(t, root, live[0])
		if len(events) != 2 || events[0].Promotion == nil || events[1].Promotion == nil {
			t.Fatalf("journaled promotion events = %+v, want the re-watch failure then the kill-switch revert", events)
		}
		if events[0].Promotion.Outcome != "applied" || !strings.HasPrefix(events[0].Promotion.Reason, promote.RewatchReasonPrefix) ||
			!strings.Contains(events[0].Promotion.Reason, "revert failed") {
			t.Fatalf("first event = %+v, want applied / re-watch revert failed", events[0].Promotion)
		}
		if events[1].Promotion.Outcome != "reverted" || events[1].Promotion.Reason != promote.KillSwitchReason {
			t.Fatalf("second event = %+v, want reverted with reason kill_switch", events[1].Promotion)
		}
		if rev.calls != 2 {
			t.Fatalf("revert attempts = %d, want 2", rev.calls)
		}
	})
}

type rewatchFlagServiceError string

func (e rewatchFlagServiceError) Error() string { return string(e) }

const errRewatchFlagService = rewatchFlagServiceError("flag service unavailable")
