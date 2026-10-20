package promote

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
)

// promotion_watch_uc043_test.go covers the autonomous promotion's watch window
// (UC-043 step 7, #1702: only real games that started after the write count) and
// the kill-switch revert (UC-043 A1, UC-045 step 5, #1704: the kill switch reverts
// an applied promotion at once and that revert is its last write).

const watchObjective = "endurance"

// recordReal stores one finalized real-game sample that started at started.
func recordReal(store *experiment.FitnessStore, gameID string, fitness float64, started time.Time) {
	store.Record(experiment.FitnessSample{
		GameID:    gameID,
		Fitness:   fitness,
		Objective: watchObjective,
		GameKind:  experiment.GameKindReal,
		StartedAt: started,
	})
}

// storeWatchedPromoter builds a Promoter whose watch reads the real FitnessStore
// (the production RealGameFitness) and whose clock is fixed at writeAt.
func storeWatchedPromoter(t *testing.T, store *experiment.FitnessStore, writeAt time.Time, realDef RealDefaultWriter, rev RealDefaultReverter) *Promoter {
	t.Helper()
	src := NewStoreRealGameFitness(store, func(string) string { return watchObjective })
	w := NewFitnessWatcher(src, DefaultWatchPolicy(), discardLogger())
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, w, rev)
	p.SetClock(func() time.Time { return writeAt })
	return p
}

// UC: UC-043 main
func TestUC043_Step7_WatchCountsOnlyGamesStartedAfterWrite(t *testing.T) {
	writeAt := time.Unix(10_000, 0)
	autonomous := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}

	t.Run("UC-043/main step 7 games before the write do not count", func(t *testing.T) {
		store := experiment.NewFitnessStore(0)
		// Five real games played before the promotion, far below the 0.50 baseline:
		// counted, they would revert the change on games that never ran with it.
		for i, id := range []string{"pre1", "pre2", "pre3", "pre4", "pre5"} {
			recordReal(store, id, 0.05, writeAt.Add(-time.Duration(10-i)*time.Minute))
		}
		rev := &recordingReverter{}
		p := storeWatchedPromoter(t, store, writeAt, &recordingRealDefault{}, rev)

		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, autonomous, testEvidence())

		if rev.calls != 0 {
			t.Fatalf("watch reverted on %d pre-write games (reverts=%d), want no revert", 5, rev.calls)
		}
		if res.Outcome != OutcomeApplied {
			t.Fatalf("outcome = %q (%s), want applied with an insufficient watch", res.Outcome, res.Reason)
		}
	})

	t.Run("UC-043/main step 7 games after the write are judged", func(t *testing.T) {
		store := experiment.NewFitnessStore(0)
		recordReal(store, "pre1", 0.95, writeAt.Add(-time.Minute))
		recordReal(store, "pre2", 0.95, writeAt.Add(-time.Minute))
		recordReal(store, "pre3", 0.95, writeAt.Add(-time.Minute))
		for _, id := range []string{"post1", "post2", "post3"} {
			recordReal(store, id, 0.10, writeAt.Add(time.Minute))
		}
		rev := &recordingReverter{}
		p := storeWatchedPromoter(t, store, writeAt, &recordingRealDefault{}, rev)

		res := p.Promote(context.Background(), promoteProposal, promoteVerdict, autonomous, testEvidence())

		if rev.calls != 1 || res.Outcome != OutcomeReverted {
			t.Fatalf("post-write degradation: reverts=%d outcome=%q, want 1 revert (the pre-write healthy games must not mask it)", rev.calls, res.Outcome)
		}
	})

	t.Run("UC-043/main step 7 a game with an unknown start does not count", func(t *testing.T) {
		store := experiment.NewFitnessStore(0)
		for _, id := range []string{"u1", "u2", "u3"} {
			recordReal(store, id, 0.05, time.Time{})
		}
		got := store.RecentRealSince(watchObjective, writeAt, 5)
		if len(got) != 0 {
			t.Fatalf("RecentRealSince returned %d samples with an unknown start, want 0", len(got))
		}
	})
}

// capturingRewatcher records the scheduled re-watch so a test can drive its
// revert closure and inspect its context deterministically.
type capturingRewatcher struct {
	ctx    context.Context
	revert func(context.Context)
}

func (c *capturingRewatcher) Schedule(ctx context.Context, _ string, _ float64, _ time.Time, _ Watcher, revert func(context.Context)) {
	c.ctx = ctx
	c.revert = revert
}

// UC: UC-043 A1
// UC: UC-045 main
func TestUC043_A1_KillSwitchRevertsAppliedPromotion(t *testing.T) {
	proposal := promoteProposal
	proposal.ExperimentID = "exp-1704"
	on := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
	off := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: false}

	t.Run("UC-043/A1 watched promotion: one revert, then no writes", func(t *testing.T) {
		realDef := &recordingRealDefault{}
		rev := &recordingReverter{}
		watcher := &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, watcher, rev)
		rw := &capturingRewatcher{}
		p.SetRewatcher(rw)

		if res := p.Promote(context.Background(), proposal, promoteVerdict, on, testEvidence()); res.Outcome != OutcomeApplied {
			t.Fatalf("precondition: outcome = %q, want applied", res.Outcome)
		}
		if rw.revert == nil {
			t.Fatal("precondition: no re-watch scheduled")
		}

		got := p.RevertOnKillSwitch(context.Background())

		if len(got) != 1 {
			t.Fatalf("kill switch handled %d promotions, want 1", len(got))
		}
		if got[0].Outcome != OutcomeReverted || got[0].Reason != KillSwitchReason || got[0].ExperimentID != "exp-1704" {
			t.Fatalf("kill-switch result = %+v, want reverted / %s / exp-1704", got[0], KillSwitchReason)
		}
		if rev.calls != 1 {
			t.Fatalf("revert writes after the kill switch = %d, want 1", rev.calls)
		}
		if rw.ctx.Err() == nil {
			t.Fatal("the re-watch is still running after the kill-switch revert")
		}

		// The re-watch confirming a degradation later must not write again.
		rw.revert(context.Background())
		// A promotion while off and further kill-switch ticks write nothing.
		p.Promote(context.Background(), proposal, promoteVerdict, off, testEvidence())
		if again := p.RevertOnKillSwitch(context.Background()); len(again) != 0 {
			t.Fatalf("second kill-switch tick handled %d promotions, want 0", len(again))
		}
		if rev.calls != 1 || len(realDef.calls) != 1 {
			t.Fatalf("writes after the kill-switch revert: reverts=%d sets=%d, want 1 and 1", rev.calls, len(realDef.calls))
		}
	})

	t.Run("UC-043/A1 confirmed promotion is reverted too", func(t *testing.T) {
		rev := &recordingReverter{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{degraded: false}, rev)
		p.Promote(context.Background(), proposal, promoteVerdict, on, testEvidence())

		got := p.RevertOnKillSwitch(context.Background())

		if len(got) != 1 || rev.calls != 1 {
			t.Fatalf("confirmed promotion: handled=%d reverts=%d, want 1 and 1", len(got), rev.calls)
		}
	})

	t.Run("UC-043/A1 a watch revert leaves nothing for the kill switch", func(t *testing.T) {
		rev := &recordingReverter{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{degraded: true}, rev)
		p.Promote(context.Background(), proposal, promoteVerdict, on, testEvidence())

		if got := p.RevertOnKillSwitch(context.Background()); len(got) != 0 || rev.calls != 1 {
			t.Fatalf("after a watch revert: handled=%d reverts=%d, want 0 and 1", len(got), rev.calls)
		}
	})

	t.Run("UC-043/A1 a failed revert is retried on the next tick", func(t *testing.T) {
		rev := &recordingReverter{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{degraded: false}, rev)
		p.Promote(context.Background(), proposal, promoteVerdict, on, testEvidence())
		rev.err = errFlagService

		first := p.RevertOnKillSwitch(context.Background())
		rev.err = nil
		second := p.RevertOnKillSwitch(context.Background())

		if len(first) != 1 || first[0].Outcome != OutcomeApplied {
			t.Fatalf("failed revert result = %+v, want outcome applied", first)
		}
		if len(second) != 1 || second[0].Outcome != OutcomeReverted || rev.calls != 2 {
			t.Fatalf("retry = %+v reverts=%d, want reverted after 2 attempts", second, rev.calls)
		}
	})
}

type flagServiceError string

func (e flagServiceError) Error() string { return string(e) }

const errFlagService = flagServiceError("flag service unavailable")
