package experiment

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// #1731: on the first enable, the proposer's goroutine may declare (and start) an
// experiment while the experiment tick's Rehydrate lists the journal dir. The
// fresh PROPOSED journal is this process's own declaration, not a leftover of a
// restart, so Rehydrate must leave it alone instead of aborting it start_failed.
// UC: UC-043 A8
func TestRehydrateLeavesAnExperimentDeclaredByThisProcess_1731(t *testing.T) {
	t.Run("UC-043/A8 a declared experiment seen by the first rehydrate stays proposed and starts", func(t *testing.T) {
		root := t.TempDir()
		ctx := context.Background()
		r := NewRegistry(RegistryConfig{Root: root, MaxShadowGames: 4, Clock: fixedClock()})

		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		// The first tick's rehydrate lists the journal dir after the declare.
		ids, err := journal.List(root)
		if err != nil {
			t.Fatalf("List: %v", err)
		}
		if err := r.Rehydrate(ids); err != nil {
			t.Fatalf("Rehydrate: %v", err)
		}
		if st, _ := r.Status(id); st != StatusProposed {
			t.Fatalf("status after rehydrate = %q, want proposed (not aborted start_failed)", st)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("status after start = %q, want running", st)
		}
		view, ok := r.CompactView(id)
		if !ok || view.Status != string(StatusRunning) {
			t.Fatalf("journaled status = %q, want running", view.Status)
		}
	})
}

// #1731: Declare creates the journal file and registers the entry under one hold
// of r.mu, so Rehydrate (which loads under r.mu) never sees a declared journal
// whose entry is not registered yet. The registry clock is read inside that hold,
// right before journal.Create; the probe checks that r.mu is held at that point.
// UC: UC-043 A8
func TestDeclareCreatesJournalUnderRegistryLock_1731(t *testing.T) {
	t.Run("UC-043/A8 declare holds the registry lock while it writes the journal", func(t *testing.T) {
		r := NewRegistry(RegistryConfig{Root: t.TempDir(), MaxShadowGames: 4})
		base := time.Date(2026, 10, 21, 12, 0, 0, 0, time.UTC)
		unlockedReads := 0
		r.clock = func() time.Time {
			if r.mu.TryLock() {
				unlockedReads++
				r.mu.Unlock()
			}
			return base
		}
		if _, err := r.Declare(sampleIntent()); err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if unlockedReads != 0 {
			t.Fatalf("Declare read the clock %d time(s) without r.mu: journal.Create runs outside the lock", unlockedReads)
		}
	})
}
