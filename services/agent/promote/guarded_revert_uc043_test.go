package promote

import (
	"context"
	"strings"
	"testing"

	"github.com/joustmania/agent/flagservice"
)

// guarded_revert_uc043_test.go: the guarded DELETE of a watch or re-watch revert
// (#2236, UC-043 A17 step 3). The flag service refuses it with 409 real_game_live
// under its registration lock, which closes the window between the agent's probe
// and the DELETE; the kill switch keeps the plain DELETE, which is never refused.

// guardedReverter records plain and guarded reverts separately.
type guardedReverter struct {
	recordingReverter
	guardedCalls int
	guardedErr   error
}

func (g *guardedReverter) RevertRealDefaultUnlessRealGame(context.Context, string) error {
	g.guardedCalls++
	return g.guardedErr
}

var realGameLive = &flagservice.Error{Status: 409, Reason: flagservice.ReasonRealGameLive}

func newGuardedRig(t *testing.T, w Watcher) (*deferredRig, *guardedReverter) {
	t.Helper()
	rig := newDeferredRig(t, w)
	g := &guardedReverter{}
	rig.p.reverter = g
	return rig, g
}

// UC: UC-043 A17
// UC: UC-043 A1
// UC: UC-045 main
func TestGuardedRevert(t *testing.T) {
	ctx := context.Background()

	t.Run("UC-043/A17 a 409 real_game_live on the watch revert keeps it pending; it runs after the game", func(t *testing.T) {
		rig, g := newGuardedRig(t, fakeWatcher{degraded: true})
		rig.probe.set(false) // the probe misses a real game that registers before the DELETE
		g.guardedErr = realGameLive
		res := rig.promote(t, holdProposal("exp-g"))
		if res.Outcome != OutcomeApplied || res.Reason != DeferredRevertReason || !res.RevertPending {
			t.Fatalf("outcome = %q (%s) pending %v, want applied + deferred revert", res.Outcome, res.Reason, res.RevertPending)
		}
		if g.guardedCalls != 1 || g.calls != 0 {
			t.Fatalf("guarded %d plain %d, want one guarded DELETE and no plain one", g.guardedCalls, g.calls)
		}
		if n, _ := rig.p.DeferredStats(); n != 1 {
			t.Fatalf("waiting = %d, want the revert", n)
		}
		// Still refused at the next drain: it keeps waiting, nothing journaled.
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if n, _ := rig.p.DeferredStats(); n != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("waiting %d journal %v, want still waiting", n, rig.journal.all())
		}
		g.guardedErr = nil
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if got := rig.journal.all(); len(got) != 1 || !strings.HasPrefix(got[0], "exp-g|reverted|") {
			t.Fatalf("journal = %v", got)
		}
		if n, _ := rig.p.DeferredStats(); n != 0 || g.calls != 0 {
			t.Fatalf("waiting %d plain %d after the revert landed", n, g.calls)
		}
	})

	t.Run("UC-043/A17 a 409 on the re-watch revert keeps it pending, journaled write-ahead", func(t *testing.T) {
		rig, g := newGuardedRig(t, &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}})
		rw := &capturingRewatcher{}
		rig.p.SetRewatcher(rw)
		rig.probe.set(false)
		if res := rig.promote(t, holdProposal("exp-rg")); res.Outcome != OutcomeApplied {
			t.Fatalf("outcome = %q", res.Outcome)
		}
		g.guardedErr = realGameLive
		rw.revert(rw.ctx)
		got := rig.journal.all()
		if len(got) != 1 || !strings.HasPrefix(got[0], "exp-rg|applied|"+RewatchReasonPrefix+DeferredRevertReason) {
			t.Fatalf("journal = %v, want the write-ahead deferred revert", got)
		}
		if n, _ := rig.p.DeferredStats(); n != 1 {
			t.Fatalf("waiting = %d, want the revert", n)
		}
		g.guardedErr = nil
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if got := rig.journal.all(); len(got) != 2 || !strings.HasPrefix(got[1], "exp-rg|reverted|"+RewatchReasonPrefix) {
			t.Fatalf("journal = %v", got)
		}
	})

	t.Run("UC-043/A1 the kill switch reverts with the plain DELETE while a real game runs and takes a refused revert over", func(t *testing.T) {
		rig, g := newGuardedRig(t, fakeWatcher{degraded: true})
		rig.probe.set(false)
		g.guardedErr = realGameLive
		rig.promote(t, holdProposal("exp-k")) // the watch revert is refused and waits
		items := rig.p.RevertOnKillSwitch(ctx)
		if g.calls != 1 || len(items) != 1 || items[0].Outcome != OutcomeReverted {
			t.Fatalf("plain reverts %d items %+v, want the kill switch's plain DELETE", g.calls, items)
		}
		if g.guardedCalls != 1 {
			t.Fatalf("guarded calls = %d: the kill switch must not use the guarded DELETE", g.guardedCalls)
		}
		g.guardedErr = nil
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if g.guardedCalls != 1 || g.calls != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("guarded %d plain %d journal %v: the drain must not revert again", g.guardedCalls, g.calls, rig.journal.all())
		}
	})

	t.Run("UC-043/A17 a watch revert of a flag that is not a hold key uses the plain DELETE", func(t *testing.T) {
		rig, g := newGuardedRig(t, fakeWatcher{degraded: true})
		prop := holdProposal("exp-n")
		prop.FlagKey = "death_grace_period_ms"
		res := rig.p.Promote(ctx, prop, promoteVerdict, autoOn, holdEvidence())
		if res.Outcome != OutcomeReverted || g.calls != 1 || g.guardedCalls != 0 {
			t.Fatalf("outcome %q plain %d guarded %d", res.Outcome, g.calls, g.guardedCalls)
		}
	})
}

// UC: UC-043 A17
func TestRealDefaultWriterGuardedRevertQuery(t *testing.T) {
	t.Run("UC-043/A17 the guarded revert sends unless_real_game=true; the plain revert (kill switch) does not", func(t *testing.T) {
		svc := newPromotionService(t)
		w := writerForTest(t, svc, t.TempDir())
		ctx := context.Background()
		if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatal(err)
		}
		if err := w.RevertRealDefaultUnlessRealGame(ctx, promotedFlag); err != nil {
			t.Fatal(err)
		}
		last := svc.Calls[len(svc.Calls)-1]
		if !strings.HasPrefix(last, "DELETE game/"+promotedFlag) || !strings.Contains(last, "game_kind=real") || !strings.Contains(last, "unless_real_game=true") {
			t.Fatalf("guarded revert call = %s", last)
		}
		if err := w.SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatal(err)
		}
		if err := w.RevertRealDefault(ctx, promotedFlag); err != nil {
			t.Fatal(err)
		}
		if last := svc.Calls[len(svc.Calls)-1]; strings.Contains(last, "unless_real_game") {
			t.Fatalf("plain revert call = %s", last)
		}
	})
}
