package promote

import (
	"context"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment"
)

// rewatch_journal_uc043_test.go covers the journaling of a re-watch revert
// (UC-043 step 7 and A9, #1883): the re-watch's revert outcome reaches the
// experiment journal, a run of failures journals one event, and the re-watch's
// failure report is its own, apart from the kill switch's (#1753).

// recordingRewatchJournal records every promotion event the Promoter journals.
type recordingRewatchJournal struct {
	mu     sync.Mutex
	ids    []string
	events []experiment.PromotionResult
}

func (r *recordingRewatchJournal) record(id string, res experiment.PromotionResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ids = append(r.ids, id)
	r.events = append(r.events, res)
}

func (r *recordingRewatchJournal) snapshot() ([]string, []experiment.PromotionResult) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.ids...), append([]experiment.PromotionResult(nil), r.events...)
}

// rewatchedPromotion applies an autonomous promotion whose first watch has too
// few real games, so a re-watch is scheduled; it returns the captured re-watch.
func rewatchedPromotion(t *testing.T, rev *recordingReverter, journal *recordingRewatchJournal) (*Promoter, *capturingRewatcher) {
	t.Helper()
	watcher := &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, watcher, rev)
	rw := &capturingRewatcher{}
	p.SetRewatcher(rw)
	if journal != nil {
		p.SetRewatchJournal(journal.record)
	}
	proposal := promoteProposal
	proposal.ExperimentID = "exp-1883"
	on := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
	if res := p.Promote(context.Background(), proposal, promoteVerdict, on, testEvidence()); res.Outcome != OutcomeApplied {
		t.Fatalf("precondition: outcome = %q, want applied", res.Outcome)
	}
	if rw.revert == nil {
		t.Fatal("precondition: no re-watch scheduled")
	}
	return p, rw
}

// UC: UC-043 main
// UC: UC-043 A9
func TestUC043_Step7_RewatchRevertIsJournaled(t *testing.T) {
	t.Run("UC-043/main step 7 a re-watch revert journals reverted on its experiment", func(t *testing.T) {
		rev := &recordingReverter{}
		journal := &recordingRewatchJournal{}
		_, rw := rewatchedPromotion(t, rev, journal)

		rw.revert(context.Background())

		ids, events := journal.snapshot()
		if rev.calls != 1 {
			t.Fatalf("revert writes = %d, want 1", rev.calls)
		}
		if len(events) != 1 || ids[0] != "exp-1883" || events[0].Outcome != string(OutcomeReverted) ||
			!strings.HasPrefix(events[0].Reason, RewatchReasonPrefix) {
			t.Fatalf("journaled = %v %+v, want one reverted event on exp-1883 with the re-watch reason", ids, events)
		}
	})

	t.Run("UC-043/A9 a run of failed re-watch reverts journals one failure, then the revert", func(t *testing.T) {
		rev := &recordingReverter{err: errFlagService}
		journal := &recordingRewatchJournal{}
		_, rw := rewatchedPromotion(t, rev, journal)

		rw.revert(context.Background())
		rw.revert(context.Background())
		_, failed := journal.snapshot()
		rev.err = nil
		rw.revert(context.Background())

		if rev.calls != 3 {
			t.Fatalf("revert attempts = %d, want 3 (journaling must not change the attempts)", rev.calls)
		}
		if len(failed) != 1 || failed[0].Outcome != string(OutcomeApplied) ||
			!strings.Contains(failed[0].Reason, "revert failed") {
			t.Fatalf("after two failed re-watch reverts journaled %+v, want one applied / revert failed event", failed)
		}
		_, all := journal.snapshot()
		if len(all) != 2 || all[1].Outcome != string(OutcomeReverted) {
			t.Fatalf("journaled %+v, want the failure then one reverted event", all)
		}
	})

	t.Run("UC-043/A9 no reverter wired journals the unrolled-back default once", func(t *testing.T) {
		journal := &recordingRewatchJournal{}
		watcher := &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient}}
		p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, watcher, nil)
		rw := &capturingRewatcher{}
		p.SetRewatcher(rw)
		p.SetRewatchJournal(journal.record)
		proposal := promoteProposal
		proposal.ExperimentID = "exp-1883"
		p.Promote(context.Background(), proposal, promoteVerdict, Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

		rw.revert(context.Background())

		if _, events := journal.snapshot(); len(events) != 1 || events[0].Outcome != string(OutcomeApplied) ||
			!strings.Contains(events[0].Reason, "no real-default reverter wired") {
			t.Fatalf("journaled %+v, want one applied / no reverter event", events)
		}
	})

	t.Run("UC-043/A1 a re-watch after the kill switch reverted journals nothing", func(t *testing.T) {
		rev := &recordingReverter{}
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)

		if got := p.RevertOnKillSwitch(context.Background()); len(got) != 1 {
			t.Fatalf("precondition: kill switch handled %d promotions, want 1", len(got))
		}
		rw.revert(context.Background())

		if _, events := journal.snapshot(); len(events) != 0 || rev.calls != 1 {
			t.Fatalf("journaled %+v reverts=%d, want none and 1 (the kill switch journals its revert)", events, rev.calls)
		}
	})

	t.Run("UC-043/main step 7 a superseded promotion's re-watch journals nothing", func(t *testing.T) {
		rev := &recordingReverter{}
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)
		olderRevert := rw.revert // the newer promotion's re-watch replaces rw.revert
		newer := promoteProposal
		newer.ExperimentID = "exp-newer"
		p.Promote(context.Background(), newer, promoteVerdict, Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

		olderRevert(context.Background())

		if _, events := journal.snapshot(); len(events) != 0 || rev.calls != 0 {
			t.Fatalf("journaled %+v reverts=%d, want none and 0", events, rev.calls)
		}
	})

	t.Run("UC-043/main step 7 no journal wired: the revert still runs", func(t *testing.T) {
		rev := &recordingReverter{}
		_, rw := rewatchedPromotion(t, rev, nil)

		rw.revert(context.Background())

		if rev.calls != 1 {
			t.Fatalf("revert writes = %d, want 1", rev.calls)
		}
	})
}

// UC: UC-043 A1
// UC: UC-043 A9
func TestUC043_A9_RewatchFailureReportIsApartFromKillSwitch(t *testing.T) {
	t.Run("UC-043/A9 a journaled re-watch failure leaves the kill switch's failure report", func(t *testing.T) {
		rev := &recordingReverter{err: errFlagService}
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)

		rw.revert(context.Background())
		got := p.RevertOnKillSwitch(context.Background())

		if _, events := journal.snapshot(); len(events) != 1 || events[0].Outcome != string(OutcomeApplied) {
			t.Fatalf("re-watch journaled %+v, want one applied failure event", events)
		}
		if len(got) != 1 || got[0].Outcome != OutcomeApplied || got[0].ExperimentID != "exp-1883" {
			t.Fatalf("kill switch after a re-watch failure returned %+v, want its own failure report", got)
		}
		if rev.calls != 2 {
			t.Fatalf("revert attempts = %d, want 2", rev.calls)
		}
	})

	t.Run("UC-043/A9 a kill-switch failure leaves the re-watch's failure report", func(t *testing.T) {
		rev := &recordingReverter{err: errFlagService}
		journal := &recordingRewatchJournal{}
		p, rw := rewatchedPromotion(t, rev, journal)

		if got := p.RevertOnKillSwitch(context.Background()); len(got) != 1 || got[0].Outcome != OutcomeApplied {
			t.Fatalf("precondition: kill-switch failure returned %+v, want one applied", got)
		}
		rw.revert(context.Background())

		if _, events := journal.snapshot(); len(events) != 1 || events[0].Outcome != string(OutcomeApplied) ||
			!strings.Contains(events[0].Reason, "revert failed") {
			t.Fatalf("re-watch after a kill-switch failure journaled %+v, want its own failure event", events)
		}
	})
}
