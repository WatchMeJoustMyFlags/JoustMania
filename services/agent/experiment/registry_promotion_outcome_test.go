package experiment

import (
	"context"
	"testing"
)

// The experiment's end state follows what the promotion achieved, and the
// promotion result is journaled (D-A22, #1701): an applied promotion ends done, a
// failed one ends discarded.
// UC: UC-043 main A9
func TestPromotionOutcomeDecidesTerminalStatus(t *testing.T) {
	t.Run("UC-043/main applied promotion: done, outcome and reference journaled", func(t *testing.T) {
		applied := PromotionResult{Outcome: PromotionApplied, Reference: "https://github.com/o/r/issues/1"}
		r, id := concludeWithPromotion(t, &fakePromoter{result: applied}, &fakeTargeting{})
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done", st)
		}
		if view, _ := r.CompactView(id); view.Status != string(StatusDone) {
			t.Errorf("journal status = %q, want done", view.Status)
		}
		assertPromotionJournaled(t, r, id, applied)
	})

	t.Run("UC-043/A9 failed promotion: discarded, outcome and reason journaled", func(t *testing.T) {
		failed := PromotionResult{Outcome: PromotionDiscarded, Reason: "open issue: 502 bad gateway (mode=issue target=github)"}
		targeting := &fakeTargeting{}
		r, id := concludeWithPromotion(t, &fakePromoter{result: failed}, targeting)
		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Errorf("status = %s, want discarded after a failed promotion", st)
		}
		if view, _ := r.CompactView(id); view.Status != string(StatusDiscarded) {
			t.Errorf("journal status = %q, want discarded", view.Status)
		}
		assertPromotionJournaled(t, r, id, failed)
		if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
			t.Errorf("stripped = %v, want [%s]", got, id)
		}
	})

	t.Run("UC-043/main reverted promotion: done, outcome and reason journaled", func(t *testing.T) {
		reverted := PromotionResult{Outcome: PromotionReverted, Reason: "real fitness fell below the control mean"}
		r, id := concludeWithPromotion(t, &fakePromoter{result: reverted}, &fakeTargeting{})
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done", st)
		}
		assertPromotionJournaled(t, r, id, reverted)
	})
}

// A kill switch (AbortAll) that lands while the promotion runs keeps the
// experiment aborted: the returning promotion never overwrites the terminal status,
// and what the promotion did is still journaled (D-A22, #1701).
// UC: UC-043 A1
func TestPromotionRacedByAbortAllStaysAborted(t *testing.T) {
	t.Run("UC-043/A1 AbortAll during promotion: experiment stays aborted", func(t *testing.T) {
		applied := PromotionResult{Outcome: PromotionApplied, Reference: "local:abc"}
		promoter := &fakePromoter{result: applied}
		var r *Registry
		promoter.during = func() { r.AbortAll(context.Background(), "kill-switch: agent disabled") }

		spawner := &fakeSpawner{}
		r = newTestRegistry(t, RegistryConfig{
			Root:           t.TempDir(),
			MaxShadowGames: 4,
			Spawner:        spawner,
			Verdict:        fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 2, alwaysReport: true},
			Promoter:       promoter,
			Targeting:      &fakeTargeting{},
		})
		ctx := context.Background()
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		if err := r.Start(ctx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if r.AllocateAndSpawn(ctx) == 0 {
			t.Fatal("no games spawned")
		}
		games := append([]spawnCall(nil), spawner.calls...)
		var final Status
		for _, g := range games[:2] {
			if final, err = r.ConcludeGame(ctx, g.gameID, 0.7, 120); err != nil {
				t.Fatalf("ConcludeGame: %v", err)
			}
		}

		if final != StatusAborted {
			t.Errorf("ConcludeGame returned %s, want aborted", final)
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Errorf("status = %s, want aborted (AbortAll won the race)", st)
		}
		if view, _ := r.CompactView(id); view.Status != string(StatusAborted) {
			t.Errorf("journal status = %q, want aborted", view.Status)
		}
		assertPromotionJournaled(t, r, id, applied)
	})
}
