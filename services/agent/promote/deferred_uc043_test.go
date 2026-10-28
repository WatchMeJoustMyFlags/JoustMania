package promote

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

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

func fixed(cfg Config) ConfigResolver { return func(context.Context) Config { return cfg } }

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
		rig.p.DrainDeferred(context.Background(), fixed(autoOn)) // still live
		if len(rig.realDef.calls) != 0 {
			t.Fatal("drained while the real game still runs")
		}
		rig.probe.set(false) // the game ended
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
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
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
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
			rig.p.DrainDeferred(context.Background(), fixed(autoOn))
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
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
		if len(rig.p.PendingFlags()) != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("pending %v journal %v, want still waiting", rig.p.PendingFlags(), rig.journal.all())
		}
		rig.realDef.err = nil
		rig.p.DrainDeferred(context.Background(), fixed(autoOn))
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
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if got := rig.journal.all(); len(got) != 2 || !strings.HasPrefix(got[1], "exp-new|applied|") || len(rig.realDef.calls) != 1 {
			t.Fatalf("journal = %v calls %d", got, len(rig.realDef.calls))
		}
	})
	t.Run("UC-043/A17 an entry older than 24 h expires even while games keep running", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-e"))
		*rig.now = rig.now.Add(DeferredTTL)
		rig.p.DrainDeferred(ctx, fixed(autoOn))
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
		rig.p.DrainDeferred(ctx, fixed(autoOn))
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
		t.Run("UC-043/A17 two drain passes that see "+name+" drop the waiting write", func(t *testing.T) {
			rig := newDeferredRig(t, nil)
			rig.promote(t, holdProposal("exp-m"))
			rig.probe.set(false)
			rig.p.DrainDeferred(ctx, fixed(cfg)) // one pass may be a flagd sync flap: kept
			if got := rig.journal.all(); len(got) != 0 || len(rig.p.PendingFlags()) != 1 || len(rig.realDef.calls) != 0 {
				t.Fatalf("one pass dropped or wrote: journal %v calls %d", got, len(rig.realDef.calls))
			}
			rig.p.DrainDeferred(ctx, fixed(cfg))
			if got := rig.journal.all(); len(got) != 1 || got[0] != "exp-m|discarded|"+name || len(rig.realDef.calls) != 0 {
				t.Fatalf("journal = %v calls %d", got, len(rig.realDef.calls))
			}
		})
	}
	t.Run("UC-043/A17 a graceful shutdown journals the dropped write", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-s"))
		rig.p.DropDeferred(ctx, ReasonShutdown)
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
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if rig.rev.calls != 1 {
			t.Fatalf("revert calls = %d after the game, want 1", rig.rev.calls)
		}
		got := rig.journal.all()
		if len(got) != 2 || !strings.HasPrefix(got[0], "exp-r|applied|"+RewatchReasonPrefix+DeferredRevertReason) || !strings.HasPrefix(got[1], "exp-r|reverted|"+RewatchReasonPrefix) {
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
		rig.p.DrainDeferred(ctx, fixed(autoOn))
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
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if rig.rev.calls != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("reverts %d journal %v: the drain must not revert or journal again", rig.rev.calls, rig.journal.all())
		}
	})
}

// UC: UC-043 A17
// UC: UC-043 A15
func TestDeferredReview2235(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-043/A17 a mode flip read before a write keeps the entry waiting", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-f"))
		rig.probe.set(false)
		calls := 0
		resolve := func(context.Context) Config {
			calls++
			if calls == 1 {
				return autoOn // the pass's read
			}
			return Config{Mode: ModeIssue, Enabled: true} // the read right before the write
		}
		rig.p.DrainDeferred(ctx, resolve)
		if len(rig.realDef.calls) != 0 || len(rig.p.PendingFlags()) != 1 || len(rig.journal.all()) != 0 {
			t.Fatalf("calls %d pending %v journal %v", len(rig.realDef.calls), rig.p.PendingFlags(), rig.journal.all())
		}
	})
	t.Run("UC-043/A17 a post-revert cooldown drops the waiting write", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		rig.promote(t, holdProposal("exp-c"))
		rig.p.markReverted(holdKey)
		rig.probe.set(false)
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		got := rig.journal.all()
		if len(rig.realDef.calls) != 0 || len(got) != 1 || !strings.Contains(got[0], "exp-c|discarded|deferred re-promotion") {
			t.Fatalf("calls %d journal %v", len(rig.realDef.calls), got)
		}
	})
	t.Run("UC-043/A17 a waiting re-watch revert is journaled write-ahead", func(t *testing.T) {
		rig := newDeferredRig(t, &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}})
		rw := &capturingRewatcher{}
		rig.p.SetRewatcher(rw)
		var pending []experiment.PromotionResult
		rig.p.SetRewatchJournal(func(_ string, res experiment.PromotionResult) { pending = append(pending, res) })
		rig.probe.set(false)
		rig.promote(t, holdProposal("exp-wa"))
		rig.probe.set(true)
		rw.revert(rw.ctx)
		if len(pending) != 1 || !pending[0].RevertPending || pending[0].Outcome != string(OutcomeApplied) {
			t.Fatalf("journal = %+v, want one applied event with RevertPending", pending)
		}
	})
	t.Run("UC-043/A17 shutdown runs a waiting revert when no real game is live", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false, true} // idle at the write, live at the watch revert
		rig.promote(t, holdProposal("exp-sd"))
		rig.probe.set(false)
		rig.p.DropDeferred(ctx, ReasonShutdown)
		if got := rig.journal.all(); rig.rev.calls != 1 || len(got) != 1 || !strings.HasPrefix(got[0], "exp-sd|reverted|") {
			t.Fatalf("reverts %d journal %v", rig.rev.calls, got)
		}
	})
	t.Run("UC-043/A17 shutdown during a real game leaves the revert unrun and journals nothing", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false}
		rig.promote(t, holdProposal("exp-sl"))
		rig.p.DropDeferred(ctx, ReasonShutdown) // probe live
		if rig.rev.calls != 0 || len(rig.journal.all()) != 0 {
			t.Fatalf("reverts %d journal %v, want neither (the write-ahead event stays last)", rig.rev.calls, rig.journal.all())
		}
		if n, _ := rig.p.DeferredStats(); n != 0 {
			t.Fatalf("waiting = %d after shutdown", n)
		}
	})
	t.Run("UC-043/A17 a revert after shutdown is not queued", func(t *testing.T) {
		rig := newDeferredRig(t, &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}})
		rw := &capturingRewatcher{}
		rig.p.SetRewatcher(rw)
		rig.probe.set(false)
		rig.promote(t, holdProposal("exp-late"))
		rig.probe.set(true)
		rig.p.DropDeferred(ctx, ReasonShutdown)
		rw.revert(rw.ctx)
		if n, _ := rig.p.DeferredStats(); n != 0 || rig.rev.calls != 0 {
			t.Fatalf("waiting %d reverts %d after shutdown", n, rig.rev.calls)
		}
	})
	t.Run("UC-043/A15 a restored waiting revert runs at the first idle check only while the overlay holds its value", func(t *testing.T) {
		rev := &overlayReverter{values: map[string]string{
			holdKey:                      "20",
			"shield.min_burst_seconds":   "0.5", // a newer promotion's value
			"shield.recharge_per_second": "",    // overlay gone
		}}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, rev)
		probe := &fakeProbe{live: true}
		p.SetRealGameProbe(probe.probe)
		j := &journalRecorder{}
		p.SetRewatchJournal(j.sink)
		p.RestoreDeferredReverts(ctx, []experiment.RestoredRevert{
			{ExperimentID: "exp-old", FlagKey: holdKey, Rewatch: true, Value: 20.0},
			{ExperimentID: "exp-newer", FlagKey: "shield.min_burst_seconds", Value: 0.3},
			{ExperimentID: "exp-gone", FlagKey: "shield.recharge_per_second", Value: 0.1},
		})
		p.DrainDeferred(ctx, fixed(autoOn))
		if len(rev.reverted) != 0 {
			t.Fatal("restored revert ran during a real game")
		}
		probe.set(false)
		p.DrainDeferred(ctx, fixed(autoOn))
		if strings.Join(rev.reverted, ",") != holdKey {
			t.Fatalf("reverted %v, want only %s (a newer promotion's value survives the restart)", rev.reverted, holdKey)
		}
		got := strings.Join(j.all(), "\n")
		for _, want := range []string{
			"exp-old|reverted|" + RewatchReasonPrefix,
			"exp-newer|applied|revert not run: the overlay holds a newer promotion's value",
			"exp-gone|applied|revert not run: no promotion overlay left",
		} {
			if !strings.Contains(got, want) {
				t.Fatalf("journal %q lacks %q", got, want)
			}
		}
	})
	t.Run("UC-043/A15 an unreadable overlay keeps the restored revert waiting", func(t *testing.T) {
		rev := &overlayReverter{err: errors.New("flag service down")}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, rev)
		p.SetRealGameProbe(func(context.Context) (bool, error) { return false, nil })
		j := &journalRecorder{}
		p.SetRewatchJournal(j.sink)
		p.RestoreDeferredReverts(ctx, []experiment.RestoredRevert{{ExperimentID: "exp-u", FlagKey: holdKey, Value: 20.0}})
		p.DrainDeferred(ctx, fixed(autoOn))
		if n, _ := p.DeferredStats(); n != 1 || len(rev.reverted) != 0 || len(j.all()) != 0 {
			t.Fatalf("waiting %d reverted %v journal %v", n, rev.reverted, j.all())
		}
	})
	t.Run("UC-043/A15 a newer promotion of this run supersedes a restored revert", func(t *testing.T) {
		rig := newDeferredRig(t, nil) // the reverter cannot read overlays: only the tracking guards
		rig.p.RestoreDeferredReverts(ctx, []experiment.RestoredRevert{{ExperimentID: "exp-old", FlagKey: holdKey, Value: 15.0}})
		rig.probe.set(false)
		if res := rig.promote(t, holdProposal("exp-new")); res.Outcome != OutcomeApplied {
			t.Fatalf("outcome = %q", res.Outcome)
		}
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		got := rig.journal.all()
		if rig.rev.calls != 0 || len(got) != 1 || !strings.HasPrefix(got[0], "exp-old|applied|superseded") {
			t.Fatalf("reverts %d journal %v", rig.rev.calls, got)
		}
	})
	t.Run("UC-043/A17 a degradation of a deferred write during the next game is journaled revert_pending", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		var events []experiment.PromotionResult
		rig.p.SetRewatchJournal(func(_ string, res experiment.PromotionResult) { events = append(events, res) })
		rig.promote(t, holdProposal("exp-dw")) // live: deferred
		rig.probe.seq = []bool{false}          // idle for the pass, live again at the watch revert
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		if len(rig.realDef.calls) != 1 || rig.rev.calls != 0 || len(events) != 1 || !events[0].RevertPending {
			t.Fatalf("writes %d reverts %d journal %+v", len(rig.realDef.calls), rig.rev.calls, events)
		}
	})
	t.Run("UC-043/A17 probe errors are counted", func(t *testing.T) {
		rig := newDeferredRig(t, nil)
		reader := sdkmetric.NewManualReader()
		if err := rig.p.RegisterDeferredMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))); err != nil {
			t.Fatal(err)
		}
		rig.p.SetRealGameProbe(func(context.Context) (bool, error) { return false, errors.New("down") })
		rig.promote(t, holdProposal("exp-pe"))
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		var rm metricdata.ResourceMetrics
		if err := reader.Collect(ctx, &rm); err != nil {
			t.Fatal(err)
		}
		var total int64
		for _, sm := range rm.ScopeMetrics {
			for _, m := range sm.Metrics {
				if sum, ok := m.Data.(metricdata.Sum[int64]); ok && m.Name == MetricProbeErrors {
					for _, dp := range sum.DataPoints {
						total += dp.Value
					}
				}
			}
		}
		if total != 2 {
			t.Fatalf("%s = %d, want 2 (promotion and drain)", MetricProbeErrors, total)
		}
	})
}

// overlayReverter reverts and reads promotion overlays: values maps a flag to its
// overlay JSON ("" = none).
type overlayReverter struct {
	values   map[string]string
	err      error
	reverted []string
}

func (o *overlayReverter) RevertRealDefault(_ context.Context, flag string) error {
	o.reverted = append(o.reverted, flag)
	if o.values != nil {
		o.values[flag] = "" // the overlay is gone
	}
	return nil
}

func (o *overlayReverter) PromotionOverlay(_ context.Context, flag string) (json.RawMessage, bool, error) {
	if o.err != nil {
		return nil, false, o.err
	}
	v := o.values[flag]
	return json.RawMessage(v), v != "", nil
}

// warnCounter counts WARN records whose message contains match.
type warnCounter struct {
	mu    sync.Mutex
	match string
	n     int
}

func (w *warnCounter) Enabled(context.Context, slog.Level) bool { return true }
func (w *warnCounter) Handle(_ context.Context, r slog.Record) error {
	if r.Level == slog.LevelWarn && strings.Contains(r.Message, w.match) {
		w.mu.Lock()
		w.n++
		w.mu.Unlock()
	}
	return nil
}
func (w *warnCounter) WithAttrs([]slog.Attr) slog.Handler { return w }
func (w *warnCounter) WithGroup(string) slog.Handler      { return w }

func probeErrorsBySource(t *testing.T, reader *sdkmetric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if sum, ok := m.Data.(metricdata.Sum[int64]); ok && m.Name == MetricProbeErrors {
				for _, dp := range sum.DataPoints {
					src, _ := dp.Attributes.Value("source")
					out[src.AsString()] += dp.Value
				}
			}
		}
	}
	return out
}

// UC: UC-043 A17
// UC: UC-043 A15
func TestDeferredReview2235Round2(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-043/A17 a waiting revert replaced by another experiment's revert is cleared in the journal", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false} // exp-a: idle at the write, live at its revert
		rig.promote(t, holdProposal("exp-a"))
		rig.probe.seq = []bool{false} // exp-b: the same
		rig.promote(t, holdProposal("exp-b"))
		got := strings.Join(rig.journal.all(), "\n")
		if !strings.Contains(got, "exp-a|applied|"+ReasonRevertSuperseded) {
			t.Fatalf("journal %q lacks the clearing event of exp-a", got)
		}
		if rig.rev.calls != 0 {
			t.Fatalf("reverts = %d during the real game", rig.rev.calls)
		}
	})
	t.Run("UC-043/A17 a waiting revert whose promotion a newer one replaced is cleared at the drain", func(t *testing.T) {
		rig := newDeferredRig(t, fakeWatcher{degraded: true})
		rig.probe.seq = []bool{false}
		rig.promote(t, holdProposal("exp-a")) // revert of exp-a waits
		rig.probe.set(false)
		rig.p.SetRewatchJournal(nil) // exp-b's own (immediate) revert is not under test
		rig.promote(t, holdProposal("exp-b"))
		rig.p.SetRewatchJournal(rig.journal.sink)
		rig.p.DrainDeferred(ctx, fixed(autoOn))
		got := rig.journal.all()
		if len(got) != 1 || got[0] != "exp-a|applied|"+ReasonRevertSuperseded {
			t.Fatalf("journal = %v", got)
		}
	})
	t.Run("UC-043/A15 an unreadable overlay is counted with source overlay_read", func(t *testing.T) {
		rev := &overlayReverter{err: errors.New("flag service down")}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, rev)
		reader := sdkmetric.NewManualReader()
		if err := p.RegisterDeferredMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))); err != nil {
			t.Fatal(err)
		}
		p.SetRealGameProbe(func(context.Context) (bool, error) { return false, nil })
		p.RestoreDeferredReverts(ctx, []experiment.RestoredRevert{{ExperimentID: "exp-u", FlagKey: holdKey, Value: 20.0}})
		p.DrainDeferred(ctx, fixed(autoOn))
		if got := probeErrorsBySource(t, reader); got[ProbeErrorSourceOverlay] != 1 || got[ProbeErrorSourceProbe] != 0 {
			t.Fatalf("errors by source = %v", got)
		}
	})
	t.Run("UC-043/A17 a failing read logs a WARN at most once a minute", func(t *testing.T) {
		h := &warnCounter{match: "deferred promotion read failed"}
		p := NewPromoter(nil, nil, &recordingRealDefault{}, fakeWatcher{}, &recordingReverter{}, testRepo(), nil, slog.New(h))
		now := time.Unix(1_000, 0)
		p.SetClock(func() time.Time { return now })
		p.SetRealGameProbe(func(context.Context) (bool, error) { return false, errors.New("down") })
		for _, step := range []time.Duration{0, 30 * time.Second, 31 * time.Second} { // t=0, 30 s, 61 s
			now = now.Add(step)
			if !p.realGameLive(ctx) {
				t.Fatal("a failed probe must count as live")
			}
		}
		if h.n != 2 {
			t.Fatalf("WARNs = %d, want 2 (t=0 and t=61 s)", h.n)
		}
	})
}

// UC: UC-043 A15
func TestDeferredRestoredRevertsShareAFlag(t *testing.T) {
	ctx := context.Background()
	a := experiment.RestoredRevert{ExperimentID: "exp-a", FlagKey: holdKey, Value: 20.0}
	b := experiment.RestoredRevert{ExperimentID: "exp-b", FlagKey: holdKey, Value: 25.0}
	for name, order := range map[string][]experiment.RestoredRevert{"A then B": {a, b}, "B then A": {b, a}} {
		t.Run("UC-043/A15 two restored reverts on one flag, "+name+": only the one whose value the overlay holds runs", func(t *testing.T) {
			rev := &overlayReverter{values: map[string]string{holdKey: "25"}} // exp-b's promotion is live
			p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, rev)
			p.SetRealGameProbe(func(context.Context) (bool, error) { return false, nil })
			j := &journalRecorder{}
			p.SetRewatchJournal(j.sink)
			p.RestoreDeferredReverts(ctx, order)
			if got := j.all(); len(got) != 0 {
				t.Fatalf("restore journaled %v; the drain decides", got)
			}
			p.DrainDeferred(ctx, fixed(autoOn))
			got := strings.Join(j.all(), "\n")
			// exp-a is cleared either way: by the mismatch (A first) or because exp-b's
			// revert already removed the overlay (B first).
			if len(rev.reverted) != 1 || !strings.Contains(got, "exp-b|reverted|") ||
				!strings.Contains(got, "exp-a|applied|revert not run: ") || strings.Contains(got, "exp-a|reverted") {
				t.Fatalf("reverted %v journal %q", rev.reverted, got)
			}
		})
	}
}
