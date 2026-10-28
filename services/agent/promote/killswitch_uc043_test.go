package promote

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

// killswitch_uc043_test.go covers the stateless kill-switch revert against the
// flag service (UC-043 A1, UC-045 step 5, #1704): it reverts a promotion written
// before an agent restart, it never touches another writer's overlay, and a
// promotion write racing the kill switch is reverted on the same tick, with one
// reported revert.

func killSwitchProposal() (p Evidence, flag string) {
	ev := testEvidence()
	ev.RealDefaultValue = 3.0
	return ev, promotedFlag
}

func deleteCalls(svc *fake.Service) int {
	n := 0
	for _, c := range svc.Calls {
		if strings.HasPrefix(c, "DELETE ") {
			n++
		}
	}
	return n
}

// UC: UC-043 A1
// UC: UC-045 main
func TestUC043_A1_KillSwitchRevertsFromFlagService(t *testing.T) {
	ctx := context.Background()
	on := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}

	t.Run("UC-043/A1 a promotion written before an agent restart is reverted", func(t *testing.T) {
		svc := newPromotionService(t)
		// The previous agent run wrote the promotion overlay; this run has no memory of it.
		if err := writerForTest(t, svc, t.TempDir()).SetRealDefault(ctx, promotedFlag, 3.0); err != nil {
			t.Fatalf("precondition: %v", err)
		}
		w := writerForTest(t, svc, t.TempDir())
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, w, fakeWatcher{}, w)

		got := p.RevertOnKillSwitch(ctx)

		if len(got) != 1 || got[0].FlagKey != promotedFlag || got[0].Outcome != OutcomeReverted || got[0].Reason != KillSwitchReason {
			t.Fatalf("kill switch after restart = %+v, want one reverted / kill_switch for %s", got, promotedFlag)
		}
		if _, ok := svc.RealValue("game", promotedFlag); ok {
			t.Fatal("the promotion overlay survived the kill switch")
		}
		if again := p.RevertOnKillSwitch(ctx); len(again) != 0 {
			t.Fatalf("second tick reverted %d flags, want 0", len(again))
		}
	})

	t.Run("UC-043/A1 another writer's real overlay is never touched", func(t *testing.T) {
		svc := newPromotionService(t)
		if _, err := svc.Put(ctx, "game", promotedFlag, flagservice.Write{
			Value: 3.0, Scope: flagservice.Scope{GameKind: "real"}, Writer: "admin",
		}); err != nil {
			t.Fatalf("precondition: %v", err)
		}
		w := writerForTest(t, svc, t.TempDir())
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, w, fakeWatcher{}, w)

		got := p.RevertOnKillSwitch(ctx)
		if err := w.RevertRealDefault(ctx, promotedFlag); err != nil {
			t.Fatalf("RevertRealDefault: %v", err)
		}

		if len(got) != 0 || deleteCalls(svc) != 0 {
			t.Fatalf("kill switch reported %v and issued %d deletes, want none for an admin overlay", got, deleteCalls(svc))
		}
		if _, ok := svc.RealValue("game", promotedFlag); !ok {
			t.Fatal("the admin real overlay was removed")
		}
	})

	t.Run("UC-043/A1 a promotion whose config predates the kill switch does not write", func(t *testing.T) {
		svc := newPromotionService(t)
		w := writerForTest(t, svc, t.TempDir())
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, w, fakeWatcher{}, w)
		ev, flag := killSwitchProposal()
		prop := promoteProposal
		prop.FlagKey = flag
		cfg := on
		cfg.killEpoch, cfg.epochKnown = p.KillSwitchEpoch(), true // read with enabled still on

		p.RevertOnKillSwitch(ctx) // the switch flips; enabled may be back on by the write
		res := p.Promote(ctx, prop, promoteVerdict, cfg, ev)

		if res.Outcome != OutcomeDiscarded || len(svc.Calls) != 0 {
			t.Fatalf("stale promotion: outcome=%q calls=%v, want discarded and no write", res.Outcome, svc.Calls)
		}
	})

	t.Run("UC-043/A1 a write in flight during the kill switch is reverted once", func(t *testing.T) {
		svc := newPromotionService(t)
		w := writerForTest(t, svc, t.TempDir())
		blocking := &blockingRealDefault{inner: w, entered: make(chan struct{}), release: make(chan struct{})}
		t.Cleanup(blocking.releaseNow) // never leave the blocked write hanging
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, blocking, fakeWatcher{}, w)
		ev, flag := killSwitchProposal()
		prop := promoteProposal
		prop.FlagKey = flag

		var wg sync.WaitGroup
		var res Result
		var reported []KillSwitchRevert
		wg.Add(1)
		go func() {
			defer wg.Done()
			res = p.Promote(ctx, prop, promoteVerdict, on, ev)
		}()
		awaitEntered(t, blocking.entered)
		killDone := make(chan struct{})
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer close(killDone)
			reported = p.RevertOnKillSwitch(ctx)
		}()
		// The kill switch must wait for the write; let the PUT land only once it has
		// either finished (no serialisation: the bug) or had time to block on it.
		select {
		case <-killDone:
		case <-time.After(200 * time.Millisecond):
		}
		blocking.releaseNow()
		wg.Wait()

		if _, ok := svc.RealValue("game", promotedFlag); ok {
			t.Fatal("the in-flight promotion survived the kill switch")
		}
		reverts := 0
		if res.Outcome == OutcomeReverted && res.Reason == KillSwitchReason {
			reverts++
		}
		for _, rv := range reported {
			if rv.Outcome == OutcomeReverted {
				reverts++
			}
		}
		if reverts != 1 || deleteCalls(svc) != 1 {
			t.Fatalf("reported reverts=%d (result %+v, kill switch %+v), deletes=%d; want exactly one of each", reverts, res, reported, deleteCalls(svc))
		}
	})
}

// blockingRealDefault holds the first SetRealDefault until release is closed.
type blockingRealDefault struct {
	inner   RealDefaultWriter
	entered chan struct{}
	release chan struct{}
	once    sync.Once
	relOnce sync.Once
}

func (b *blockingRealDefault) releaseNow() { b.relOnce.Do(func() { close(b.release) }) }

func (b *blockingRealDefault) SetRealDefault(ctx context.Context, flag string, value any) error {
	b.once.Do(func() { close(b.entered) })
	<-b.release
	return b.inner.SetRealDefault(ctx, flag, value)
}

// UC: UC-043 A1
func TestUC043_A1_LostClaimJournalsOnce(t *testing.T) {
	t.Run("UC-043/A1 a revert superseded by a newer promotion is not a kill-switch revert", func(t *testing.T) {
		rev := &recordingReverter{}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, rev)
		on := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
		rw := &capturingRewatcher{}
		p.SetRewatcher(rw)
		p.watcher = &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient, VerdictHealthy}}
		p.Promote(context.Background(), promoteProposal, promoteVerdict, on, testEvidence())
		oldRevert := rw.revert
		p.Promote(context.Background(), promoteProposal, promoteVerdict, on, testEvidence()) // newer write of the flag

		var res Result
		entryOld := &appliedPromotion{proposal: promoteProposal}
		p.revertReal(context.Background(), promoteProposal, entryOld, &res, nil, false)
		oldRevert(context.Background())

		if res.Reason == KillSwitchReason || res.Outcome == OutcomeReverted {
			t.Fatalf("superseded revert result = %+v, want not reverted / not kill_switch", res)
		}
		if rev.calls != 0 {
			t.Fatalf("superseded promotion wrote %d reverts, want 0", rev.calls)
		}
	})
}

// blockingListingReverter is a RealDefaultReverter and PromotionOverlayLister whose
// first revert blocks until release is closed (#1893). It lists the promoted flag
// while its overlay stands; every successful revert removes the overlay. It never
// holds its own mutex while it blocks, so a second caller waits on the Promoter's
// locks only, never on the fake.
type blockingListingReverter struct {
	mu       sync.Mutex
	reverts  int
	overlay  bool
	firstErr error

	entered     chan struct{}
	release     chan struct{}
	releaseOnce sync.Once
}

func newBlockingListingReverter(t *testing.T, firstErr error) *blockingListingReverter {
	t.Helper()
	r := &blockingListingReverter{
		overlay:  true,
		firstErr: firstErr,
		entered:  make(chan struct{}),
		release:  make(chan struct{}),
	}
	// Never leave the blocked revert hanging, whatever path the test takes.
	t.Cleanup(r.releaseNow)
	return r
}

func (r *blockingListingReverter) releaseNow() {
	r.releaseOnce.Do(func() { close(r.release) })
}

func (r *blockingListingReverter) RevertRealDefault(context.Context, string) error {
	r.mu.Lock()
	r.reverts++
	first := r.reverts == 1
	r.mu.Unlock()
	if first {
		close(r.entered)
		<-r.release
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if first && r.firstErr != nil {
		return r.firstErr
	}
	r.overlay = false
	return nil
}

func (r *blockingListingReverter) PromotedFlags(context.Context) ([]string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.overlay {
		return []string{promoteProposal.FlagKey}, nil
	}
	return nil, nil
}

func (r *blockingListingReverter) revertCount() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.reverts
}

// awaitEntered fails the test instead of hanging when the blocked call is never reached.
func awaitEntered(t *testing.T, entered <-chan struct{}) {
	t.Helper()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("revert never reached the flag service")
	}
}

// killSwitchDuringRevert runs revert in the background, starts a kill-switch tick
// once the revert is inside the flag-service call, lets the revert finish only once
// the tick has either finished (no serialisation: the bug) or had time to block on
// it, and returns what the tick reported.
func killSwitchDuringRevert(t *testing.T, p *Promoter, rev *blockingListingReverter, revert func()) []KillSwitchRevert {
	revertDone := make(chan struct{})
	go func() {
		defer close(revertDone)
		revert()
	}()
	awaitEntered(t, rev.entered)
	var reported []KillSwitchRevert
	killDone := make(chan struct{})
	go func() {
		defer close(killDone)
		reported = p.RevertOnKillSwitch(context.Background())
	}()
	select {
	case <-killDone:
	case <-time.After(200 * time.Millisecond):
	}
	rev.releaseNow()
	<-revertDone
	<-killDone
	return reported
}

// assertKillMuHeldWhileReverting fails when the kill switch's lock is free while a
// watch or re-watch revert is inside the flag-service call.
func assertKillMuHeldWhileReverting(t *testing.T, p *Promoter, rev *blockingListingReverter, revert func()) {
	t.Helper()
	done := make(chan struct{})
	go func() {
		defer close(done)
		revert()
	}()
	awaitEntered(t, rev.entered)
	held := !p.killMu.TryLock()
	if !held {
		p.killMu.Unlock()
	}
	rev.releaseNow()
	<-done
	if !held {
		t.Fatal("killMu is free while a revert is in flight: a kill-switch tick can revert the overlay a second time")
	}
}

// UC: UC-043 A1
// UC: UC-043 A9
func TestUC043_A1_RewatchRevertSerialisedWithKillSwitch(t *testing.T) {
	ctx := context.Background()

	t.Run("UC-043/A1 a re-watch revert in flight holds the kill switch", func(t *testing.T) {
		rev := newBlockingListingReverter(t, nil)
		p, rw := rewatchedPromotion(t, rev, &recordingRewatchJournal{})

		assertKillMuHeldWhileReverting(t, p, rev, func() { rw.revert(ctx) })
	})

	t.Run("UC-043/A1 a sync watch revert in flight holds the kill switch", func(t *testing.T) {
		rev := newBlockingListingReverter(t, nil)
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{degraded: true}, rev)
		on := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
		var res Result

		assertKillMuHeldWhileReverting(t, p, rev, func() {
			res = p.Promote(ctx, promoteProposal, promoteVerdict, on, testEvidence())
		})

		if res.Outcome != OutcomeReverted {
			t.Fatalf("watch outcome = %q, want reverted", res.Outcome)
		}
	})

	t.Run("UC-043/A1 a kill-switch tick during a re-watch revert reverts and reports nothing again", func(t *testing.T) {
		rev := newBlockingListingReverter(t, nil)
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)

		reported := killSwitchDuringRevert(t, p, rev, func() { rw.revert(ctx) })

		if n := rev.revertCount(); n != 1 {
			t.Fatalf("revert writes = %d, want 1 (the re-watch's only)", n)
		}
		if len(reported) != 0 {
			t.Fatalf("kill switch reported %+v, want nothing: the re-watch reverted the overlay", reported)
		}
		ids, events := journal.snapshot()
		if len(events) != 1 || ids[0] != "exp-1883" || events[0].Outcome != string(OutcomeReverted) ||
			!strings.HasPrefix(events[0].Reason, RewatchReasonPrefix) {
			t.Fatalf("journaled %v %+v, want one re-watch reverted event on exp-1883", ids, events)
		}
	})

	t.Run("UC-043/A9 a failed re-watch revert during a tick keeps its experiment for the kill switch", func(t *testing.T) {
		rev := newBlockingListingReverter(t, errFlagService)
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)

		reported := killSwitchDuringRevert(t, p, rev, func() { rw.revert(ctx) })

		if n := rev.revertCount(); n != 2 {
			t.Fatalf("revert attempts = %d, want 2 (one re-watch, one kill switch)", n)
		}
		if len(reported) != 1 || reported[0].ExperimentID != "exp-1883" ||
			reported[0].Outcome != OutcomeReverted || reported[0].Reason != KillSwitchReason {
			t.Fatalf("kill switch reported %+v, want one reverted / kill_switch on exp-1883", reported)
		}
		ids, events := journal.snapshot()
		if len(events) != 1 || ids[0] != "exp-1883" || events[0].Outcome != string(OutcomeApplied) ||
			!strings.HasPrefix(events[0].Reason, RewatchReasonPrefix) {
			t.Fatalf("journaled %v %+v, want one re-watch applied event on exp-1883", ids, events)
		}
	})
}
