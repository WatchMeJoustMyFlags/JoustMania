package promote

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
)

// deferred_uc043_test.go: the deferred real write of a hold key (UC-043 A17 step 3,
// FR-162, #2096 PR-7b-2b). The maintainer rule: no real write of a shield.* key and
// no agent revert of one lands while a real game runs; it lands after the game
// ends. Only the kill switch reverts at any time.

// fakeProbe is a settable real-game probe.
type fakeProbe struct {
	mu   sync.Mutex
	live bool
	err  error
	seq  []bool // consumed first, then live
}

func (f *fakeProbe) set(live bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.live = live
}

func (f *fakeProbe) probe(context.Context) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.seq) > 0 {
		v := f.seq[0]
		f.seq = f.seq[1:]
		return v, nil
	}
	return f.live, f.err
}

// journalRecorder records the later promotion outcomes per experiment.
type journalRecorder struct {
	mu     sync.Mutex
	events []string // "<experiment>|<outcome>|<reason>"
}

func (j *journalRecorder) sink(id string, res experiment.PromotionResult) {
	j.mu.Lock()
	defer j.mu.Unlock()
	j.events = append(j.events, id+"|"+res.Outcome+"|"+res.Reason)
}

func (j *journalRecorder) all() []string {
	j.mu.Lock()
	defer j.mu.Unlock()
	return append([]string(nil), j.events...)
}

const holdKey = "shield.energy_budget_seconds"

var (
	autoOn = Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
)

func holdProposal(id string) experiment.Proposal {
	return experiment.Proposal{FlagKey: holdKey, ExperimentID: id, ExperimentalValue: 20.0}
}

func holdEvidence() Evidence {
	ev := testEvidence()
	ev.RealDefaultValue = 20.0
	return ev
}

type deferredRig struct {
	p       *Promoter
	realDef *recordingRealDefault
	rev     *recordingReverter
	probe   *fakeProbe
	journal *journalRecorder
	now     *time.Time
}

func newDeferredRig(t *testing.T, w Watcher) *deferredRig {
	t.Helper()
	rig := &deferredRig{realDef: &recordingRealDefault{}, rev: &recordingReverter{}, probe: &fakeProbe{live: true}, journal: &journalRecorder{}}
	if w == nil {
		w = fakeWatcher{}
	}
	rig.p, _ = promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, rig.realDef, w, rig.rev)
	now := time.Unix(100_000, 0)
	rig.now = &now
	rig.p.SetClock(func() time.Time { return *rig.now })
	rig.p.SetRealGameProbe(rig.probe.probe)
	rig.p.SetRewatchJournal(rig.journal.sink)
	return rig
}

func (r *deferredRig) promote(t *testing.T, prop experiment.Proposal) Result {
	t.Helper()
	return r.p.Promote(context.Background(), prop, promoteVerdict, autoOn, holdEvidence())
}

// UC: UC-043 A17
// UC: UC-040 BR-027
func TestDeferredRealWrite_NoWriteWhileRealGameRuns(t *testing.T) {
	t.Run("UC-043/A17 no RealDefault write for a hold key while a real game is RUNNING; it lands after game end", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		res := rig.promote(t, holdProposal("exp-a"))
		if res.Outcome != OutcomeDeferred || res.Reason != DeferredReason {
			t.Fatalf("outcome = %q (%s), want deferred", res.Outcome, res.Reason)
		}
		if len(rig.realDef.calls) != 0 {
			t.Fatalf("SetRealDefault calls = %d while the real game runs, want 0", len(rig.realDef.calls))
		}
		if got := rig.p.PendingFlags(); len(got) != 1 || got[0] != holdKey {
			t.Fatalf("pending = %v", got)
		}
		rig.p.DrainDeferred(context.Background(), autoOn) // still live
		if len(rig.realDef.calls) != 0 {
			t.Fatal("drained while the real game still runs")
		}
		rig.probe.set(false) // the game ended
		rig.p.DrainDeferred(context.Background(), autoOn)
		if len(rig.realDef.calls) != 1 || rig.realDef.calls[0].flag != holdKey || rig.realDef.calls[0].value != 20.0 {
			t.Fatalf("calls = %+v, want one write of 20", rig.realDef.calls)
		}
		if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-a|applied|"+DeferredAppliedReason {
			t.Fatalf("journal = %v", got)
		}
		if len(rig.p.PendingFlags()) != 0 {
			t.Fatal("queue not empty after the write")
		}
	})
	t.Run("UC-043/A17 a game that starts again before the drain keeps it waiting", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-b"))
		rig.probe.set(false)
		rig.probe.set(true)
		rig.p.DrainDeferred(context.Background(), autoOn)
		if len(rig.realDef.calls) != 0 {
			t.Fatal("written while a new real game runs")
		}
	})
	for name, probe := range map[string]RealGameProbe{
		"no probe":     nil,
		"failed probe": func(context.Context) (bool, error) { return false, errors.New("boom") },
	} {
		t.Run("UC-043/A17 "+name+" counts as live (fail closed)", func(t *testing.T) {
			rig := newDeferredRig(t, nil)
			rig.p.SetRealGameProbe(probe)
			if res := rig.promote(t, holdProposal("exp-c")); res.Outcome != OutcomeDeferred {
				t.Fatalf("outcome = %q, want deferred", res.Outcome)
			}
			rig.p.DrainDeferred(context.Background(), autoOn)
			if len(rig.realDef.calls) != 0 {
				t.Fatal("written without a working probe")
			}
		})
	}
	t.Run("UC-043/A17 a non-hold key is written at once while a real game runs", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		res := rig.p.Promote(context.Background(), promoteProposal, promoteVerdict, autoOn, testEvidence())
		if res.Outcome != OutcomeApplied || len(rig.realDef.calls) != 1 {
			t.Fatalf("outcome = %q calls %d, want applied once", res.Outcome, len(rig.realDef.calls))
		}
	})
}

// UC: UC-043 A17
func TestDeferredRealWrite_FlagServiceGuard(t *testing.T) {
	conflict := &flagservice.Error{Status: 409, Reason: flagservice.ReasonRealGameLive}
	t.Run("UC-043/A17 409 real_game_live on the promotion write gives deferred, not discarded", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.probe.set(false) // the prefilter missed a game that registered
		rig.realDef.err = conflict
		res := rig.promote(t, holdProposal("exp-d"))
		if res.Outcome != OutcomeDeferred {
			t.Fatalf("outcome = %q (%s), want deferred", res.Outcome, res.Reason)
		}
		// The drain hits the guard again: the entry keeps waiting.
		rig.p.DrainDeferred(context.Background(), autoOn)
		if len(rig.p.PendingFlags()) != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("pending %v journal %v, want still waiting", rig.p.PendingFlags(), rig.journal.all())
		}
		rig.realDef.err = nil
		rig.p.DrainDeferred(context.Background(), autoOn)
		if got := rig.journal.all(); len(got) != 1 || !strings.HasPrefix(got[0], "exp-d|applied|") {
			t.Fatalf("journal = %v", got)
		}
	})
}

// UC: UC-043 A17
// UC: UC-043 A1
// UC: UC-045 main
func TestDeferredRealWrite_Dropped(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-043/A17 a newer promotion of the flag supersedes the waiting one", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-old"))
		rig.promote(t, holdProposal("exp-new"))
		if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-old|discarded|"+ReasonSuperseded {
			t.Fatalf("journal = %v", got)
		}
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, autoOn)
		if got := rig.journal.all(); len(got) != 2 || !strings.HasPrefix(got[1], "exp-new|applied|") || len(rig.realDef.calls) != 1 {
			t.Fatalf("journal = %v calls %d", got, len(rig.realDef.calls))
		}
	})
	t.Run("UC-043/A17 an entry older than 24 h expires even while games keep running", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-e"))
		*rig.now = rig.now.Add(DeferredTTL)
		rig.p.DrainDeferred(ctx, autoOn)
		if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-e|discarded|"+ReasonDeferredExpired {
			t.Fatalf("journal = %v", got)
		}
	})
	t.Run("UC-043/A1 the kill switch drops the waiting write: no write afterwards", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-k"))
		items := rig.p.RevertOnKillSwitch(ctx)
		if len(items) != 1 || items[0].ExperimentID != "exp-k" || items[0].Outcome != OutcomeDiscarded || items[0].Reason != KillSwitchReason {
			t.Fatalf("kill switch items = %+v", items)
		}
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, autoOn)
		if len(rig.realDef.calls) != 0 {
			t.Fatal("written after the kill switch")
		}
	})
	t.Run("UC-043/A17 a promotion whose config predates a kill-switch tick is not queued", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		cfg := autoOn
		cfg.killEpoch, cfg.epochKnown = rig.p.KillSwitchEpoch(), true
		rig.p.RevertOnKillSwitch(ctx)
		res := rig.p.Promote(ctx, holdProposal("exp-stale"), promoteVerdict, cfg, holdEvidence())
		if res.Outcome != OutcomeDiscarded || len(rig.p.PendingFlags()) != 0 {
			t.Fatalf("outcome = %q pending %v, want discarded and nothing queued", res.Outcome, rig.p.PendingFlags())
		}
	})
	for name, cfg := range map[string]Config{
		ReasonModeChanged: {Mode: ModeIssue, Target: TargetLocal, Enabled: true},
		KillSwitchReason:  {Mode: ModeAutonomous, Target: TargetLocal, Enabled: false},
	} {
		t.Run("UC-043/A17 a drain that sees "+name+" drops the waiting write", func(t *testing.T) {
			rig := newDeferredRig(t, nil)
			rig.promote(t, holdProposal("exp-m"))
			rig.probe.set(false)
			rig.p.DrainDeferred(ctx, cfg)
			if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-m|discarded|"+name || len(rig.realDef.calls) != 0 {
				t.Fatalf("journal = %v calls %d", got, len(rig.realDef.calls))
			}
		})
	}
	t.Run("UC-043/A17 a graceful shutdown journals the dropped write", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-s"))
		rig.p.DropDeferred(ReasonShutdown)
		if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-s|discarded|"+ReasonShutdown {
			t.Fatalf("journal = %v", got)
		}
	})
	t.Run("UC-043/A17 the gauges' stats count waiting entries and their age", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-g"))
		*rig.now = rig.now.Add(90 * time.Second)
		if n, oldest := rig.p.DeferredStats(); n != 1 || oldest != 90*time.Second {
			t.Fatalf("stats = %d, %s", n, oldest)
		}
	})
}

// UC: UC-043 A17
func TestDeferredRevert_WatchRevertsWaitForGameEnd(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-043/A17 a re-watch revert of a hold key waits while a real game runs", func(t *testing.T) {
		rig := newDeferredRig(t, &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}})
		rw := &capturingRewatcher{}
		rig.p.SetRewatcher(rw)
		rig.probe.set(false)
		if res := rig.promote(t, holdProposal("exp-r")); res.Outcome != OutcomeApplied {
			t.Fatalf("outcome = %q", res.Outcome)
		}
		rig.probe.set(true) // the next real game runs when the re-watch confirms degradation
		rw.revert(rw.ctx)
		if rig.rev.calls != 0 {
			t.Fatal("reverted mid-game")
		}
		if n, _ := rig.p.DeferredStats(); n != 1 {
			t.Fatalf("waiting = %d, want the revert", n)
		}
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, autoOn)
		if rig.rev.calls != 1 {
			t.Fatalf("revert calls = %d after the game, want 1", rig.rev.calls)
		}
		got := rig.journal.all()
		if len(got) != 1 || !strings.HasPrefix(got[0], "exp-r|reverted|"+RewatchReasonPrefix) {
			t.Fatalf("journal = %v", got)
		}
	})
	t.Run("UC-043/A17 a watch revert in the promotion call waits; the result keeps applied", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false} // idle at the write, live from the revert on
		res := rig.promote(t, holdProposal("exp-w"))
		if res.Outcome != OutcomeApplied || res.Reason != DeferredRevertReason || rig.rev.calls != 0 {
			t.Fatalf("outcome = %q (%s) reverts %d", res.Outcome, res.Reason, rig.rev.calls)
		}
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, autoOn)
		if got := rig.journal.all(); rig.rev.calls != 1 || len(got) != 1 || !strings.HasPrefix(got[0], "exp-w|reverted|") {
			t.Fatalf("reverts %d journal %v", rig.rev.calls, got)
		}
	})
	t.Run("UC-043/A1 the kill switch reverts at once and takes the waiting revert over", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false}
		rig.promote(t, holdProposal("exp-x"))
		items := rig.p.RevertOnKillSwitch(ctx) // a real game still runs
		if rig.rev.calls != 1 || len(items) != 1 || items[0].Outcome != OutcomeReverted {
			t.Fatalf("kill switch reverts %d items %+v", rig.rev.calls, items)
		}
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, autoOn)
		if rig.rev.calls != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("reverts %d journal %v: the drain must not revert or journal again", rig.rev.calls, rig.journal.all())
		}
	})
}
