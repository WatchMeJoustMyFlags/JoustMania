package experiment

import (
	"context"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// concludeWithPromotion runs one experiment to a promote verdict through promoter
// and returns the registry and the experiment id.
func concludeWithPromotion(t *testing.T, promoter *fakePromoter, targeting *fakeTargeting) (*Registry, string) {
	t.Helper()
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		Root:           t.TempDir(),
		MaxShadowGames: 4,
		Spawner:        spawner,
		Verdict:        fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 2, alwaysReport: true},
		Promoter:       promoter,
		Targeting:      targeting,
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
	for _, g := range games[:2] {
		if _, err := r.ConcludeGame(ctx, g.gameID, 0.7, 120); err != nil {
			t.Fatalf("ConcludeGame: %v", err)
		}
	}
	if promoter.count() != 1 {
		t.Fatalf("promoter called %d times, want 1", promoter.count())
	}
	return r, id
}

// assertPromotionJournaled checks the experiment journal holds exactly one
// promotion event carrying want (#1701).
func assertPromotionJournaled(t *testing.T, r *Registry, id string, want PromotionResult) {
	t.Helper()
	evs := eventsOfKind(journalEvents(t, r, id), journal.KindPromotion)
	if len(evs) != 1 || evs[0].Promotion == nil {
		t.Fatalf("promotion events = %+v, want exactly one with a result", evs)
	}
	if got := *evs[0].Promotion; got != want {
		t.Errorf("journaled promotion = %+v, want %+v", got, want)
	}
}

// An inert promotion (the env gates are not set, stock stack) is a promotion that
// could not act: the promoter returns outcome discarded with a reason. The
// experiment still runs step 8 (terminal, targeting stripped) and ends discarded,
// with the promotion outcome and reason in its journal (#1701).
// UC: UC-043 A4
func TestPromotionInertStillTearsDown(t *testing.T) {
	t.Run("UC-043/A4 inert promotion: experiment ends discarded, outcome journaled, targeting stripped", func(t *testing.T) {
		inert := PromotionResult{Outcome: PromotionDiscarded, Reason: "github target not enabled (mode=pr target=github)"}
		targeting := &fakeTargeting{}
		r, id := concludeWithPromotion(t, &fakePromoter{result: inert}, targeting)

		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Errorf("status = %s, want discarded after an inert promotion", st)
		}
		if view, _ := r.CompactView(id); view.Status != string(StatusDiscarded) {
			t.Errorf("journal status = %q, want discarded", view.Status)
		}
		assertPromotionJournaled(t, r, id, inert)
		if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
			t.Errorf("stripped = %v, want [%s] (teardown after a no-op promotion)", got, id)
		}
	})
}
