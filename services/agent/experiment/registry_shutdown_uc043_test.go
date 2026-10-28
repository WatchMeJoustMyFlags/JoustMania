package experiment

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// registry_shutdown_uc043_test.go (#2332): once the agent's graceful stop has begun
// (the registry's shutdown context is cancelled), a conclusion still journals the
// game's sample and the verdict but never calls the promoter, and no experiment is
// declared or started, so a proposer racing the shutdown AbortAll starts nothing.

func stoppedContext() context.Context {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return ctx
}

// UC: UC-043 A20
func TestUC043_A20_ConclusionAtShutdownDoesNotPromote(t *testing.T) {
	t.Run("UC-043/A20 a promote verdict that lands after the stop began is journaled, not promoted", func(t *testing.T) {
		promoter := &fakePromoter{result: PromotionResult{Outcome: PromotionApplied, Reference: "local:abc"}}
		spawner := &fakeSpawner{}
		targeting := &fakeTargeting{}
		r := newTestRegistry(t, RegistryConfig{
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
		r.SetShutdownContext(stoppedContext()) // the SIGTERM analogue

		var final Status
		for _, g := range games[:2] {
			if final, err = r.ConcludeGame(ctx, g.gameID, 0.7, 120); err != nil {
				t.Fatalf("ConcludeGame: %v", err)
			}
		}

		if n := promoter.count(); n != 0 {
			t.Fatalf("promoter called %d time(s) after the stop began, want 0", n)
		}
		if final != StatusDiscarded {
			t.Errorf("ConcludeGame returned %s, want discarded", final)
		}
		evs := journalEvents(t, r, id)
		if got := len(eventsOfKind(evs, journal.KindGameConcluded)); got != 2 {
			t.Errorf("game_concluded events = %d, want 2 (the samples are kept)", got)
		}
		if view, _ := r.CompactView(id); view.Verdict == nil || view.Verdict.Outcome != CohortOutcomePromote {
			t.Errorf("journal verdict = %+v, want the promote verdict kept", view.Verdict)
		}
		for _, ev := range evs {
			if ev.Kind == journal.KindDecision && ev.Status == string(StatusPromoting) {
				t.Errorf("journal has a promoting decision %+v, want none", ev)
			}
		}
		assertPromotionJournaled(t, r, id, PromotionResult{Outcome: PromotionDiscarded, Reason: PromotionReasonShutdown})
		if got := targeting.strippedIDs(); len(got) != 1 || got[0] != id {
			t.Errorf("stripped = %v, want [%s]", got, id)
		}
	})

	t.Run("UC-043/A20 a live shutdown context still promotes", func(t *testing.T) {
		promoter := &fakePromoter{result: PromotionResult{Outcome: PromotionApplied, Reference: "local:abc"}}
		concludeWithPromotionCtx(t, promoter, context.Background())
		if n := promoter.count(); n != 1 {
			t.Fatalf("promoter called %d time(s), want 1", n)
		}
	})
}

// concludeWithPromotionCtx runs one experiment to a promote verdict with the
// registry's shutdown context set to stop.
func concludeWithPromotionCtx(t *testing.T, promoter *fakePromoter, stop context.Context) {
	t.Helper()
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 4,
		Spawner:        spawner,
		Verdict:        fakeVerdict{outcome: CohortOutcomePromote, concludeAtN: 2, alwaysReport: true},
		Promoter:       promoter,
		Targeting:      &fakeTargeting{},
	})
	r.SetShutdownContext(stop)
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
	for _, g := range append([]spawnCall(nil), spawner.calls...)[:2] {
		if _, err := r.ConcludeGame(ctx, g.gameID, 0.7, 120); err != nil {
			t.Fatalf("ConcludeGame: %v", err)
		}
	}
}

// UC: UC-043 A20
func TestUC043_A20_NoExperimentStartsAfterShutdown(t *testing.T) {
	t.Run("UC-043/A20 declare after the stop began is refused and leaves no journal", func(t *testing.T) {
		root := t.TempDir()
		r := newTestRegistry(t, RegistryConfig{Root: root, Spawner: &fakeSpawner{}, Targeting: &fakeTargeting{}})
		r.SetShutdownContext(stoppedContext())

		id, err := r.Declare(sampleIntent())

		if !errors.Is(err, ErrShuttingDown) {
			t.Fatalf("Declare = (%q, %v), want ErrShuttingDown", id, err)
		}
		if ids, _ := journal.List(root); len(ids) != 0 {
			t.Errorf("journals = %v, want none", ids)
		}
		if n := r.LiveCount(); n != 0 {
			t.Errorf("live experiments = %d, want 0", n)
		}
	})

	t.Run("UC-043/A20 start after the stop began writes no targeting", func(t *testing.T) {
		targeting := &fakeTargeting{}
		r := newTestRegistry(t, RegistryConfig{Spawner: &fakeSpawner{}, Targeting: targeting})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		r.SetShutdownContext(stoppedContext())

		if err := r.Start(context.Background(), id); !errors.Is(err, ErrShuttingDown) {
			t.Fatalf("Start = %v, want ErrShuttingDown", err)
		}
		targeting.mu.Lock()
		applied := append([]string(nil), targeting.applied...)
		targeting.mu.Unlock()
		if len(applied) != 0 {
			t.Errorf("targeting applied for %v, want none", applied)
		}
		if st, _ := r.Status(id); st == StatusRunning {
			t.Errorf("status = %s, want not running", st)
		}
	})

	t.Run("UC-043/A20 a proposal reviewed after the stop began starts nothing", func(t *testing.T) {
		targeting := &fakeTargeting{}
		r := newTestRegistry(t, RegistryConfig{Spawner: &fakeSpawner{}, Targeting: targeting})
		r.SetShutdownContext(stoppedContext())
		sink := &RegistrySink{Registry: r, Objective: "balanced"}

		err := sink.Review(context.Background(), Proposal{FlagKey: "death_grace_period_seconds", ExperimentalValue: 0.5, Rationale: "r"})

		if !errors.Is(err, ErrShuttingDown) {
			t.Fatalf("Review = %v, want ErrShuttingDown", err)
		}
		if n := r.LiveCount(); n != 0 {
			t.Errorf("live experiments = %d, want 0", n)
		}
	})
}
