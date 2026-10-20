package experiment

import (
	"context"
	"errors"
	"testing"
)

// An inert promotion (the env gates are not set, stock stack) makes the promoter
// return the recorded no-op error. The experiment must still run step 8: reach a
// terminal status and have its targeting stripped, instead of lingering with the
// arm targeting applied.
// UC: UC-043 A4
func TestPromotionInertStillTearsDown(t *testing.T) {
	t.Run("UC-043/A4 promoter returns the recorded no-op error: experiment ends terminal, targeting stripped", func(t *testing.T) {
		spawner := &fakeSpawner{}
		promoter := &fakePromoter{retErr: errors.New("promote: recorded no-op: github target not enabled")}
		targeting := &fakeTargeting{}
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
		// The recorded no-op error is non-fatal: the promotion is still routed (done),
		// not discarded.
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done after an inert promotion", st)
		}
		if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
			t.Errorf("stripped = %v, want [%s] (teardown after a no-op promotion)", got, id)
		}
	})
}
