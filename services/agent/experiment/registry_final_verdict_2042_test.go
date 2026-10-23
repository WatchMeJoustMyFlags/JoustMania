package experiment

import (
	"context"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
)

// promoteFromCall returns inconclusive until its n-th evaluation, then promote.
type promoteFromCall struct {
	mu    sync.Mutex
	calls int
	at    int
}

func (p *promoteFromCall) Evaluate(journal.Summary) (journal.Verdict, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.calls++
	if p.calls >= p.at {
		return journal.Verdict{Outcome: CohortOutcomePromote, Significant: true, Delta: 0.2, Reason: "final promote (test)"}, true
	}
	return journal.Verdict{Outcome: OutcomeInconclusive, Reason: "interim (test)"}, true
}

// verdictPromoter records the verdict on the view the promoter is handed.
type verdictPromoter struct {
	mu       sync.Mutex
	verdicts []*journal.Verdict
}

func (v *verdictPromoter) Promote(_ context.Context, view journal.CompactView) PromotionResult {
	v.mu.Lock()
	defer v.mu.Unlock()
	v.verdicts = append(v.verdicts, view.Verdict)
	return PromotionResult{Outcome: PromotionApplied, Reference: "local:fake"}
}

// The final verdict is journaled with the conclusion (#2042), so the promoter reads
// the verdict the experiment concluded on, not the last interim one.
// UC: UC-043 BR-006
// UC: UC-046 A6
func TestFinalVerdictJournaledAtConclusion_2042(t *testing.T) {
	t.Run("UC-043/BR-006 a pattern-exhausted promote reaches the promoter as promote", func(t *testing.T) {
		spawner := &patternSpawner{}
		promoter := &verdictPromoter{}
		// Four ConcludeGame evaluations are interim inconclusive; the fifth, the
		// pattern-exhausted conclusion (UC-046 A6), is promote and is not journaled
		// as an interim verdict.
		r := newTestRegistry(t, RegistryConfig{
			MaxShadowGames: 4, EffectiveConcurrency: 4, MaxGamesPerExperiment: -1,
			Spawner: spawner, Verdict: &promoteFromCall{at: 5}, Promoter: promoter, Targeting: &fakeTargeting{},
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "arch_a", "arch_b")}},
		})
		intent := sampleIntent()
		intent.TargetNPerArm = 100 // keep the give-up guard out of the way
		id, err := r.Declare(intent)
		if err != nil {
			t.Fatal(err)
		}
		ctx := context.Background()
		if err := r.Start(ctx, id); err != nil {
			t.Fatal(err)
		}
		for game := 0; game < 4; game++ {
			if n := r.AllocateAndSpawn(ctx); n != 1 {
				t.Fatalf("game %d: spawned %d, want 1", game, n)
			}
			spawner.fakeSpawner.mu.Lock()
			gameID := spawner.fakeSpawner.calls[game].gameID
			spawner.fakeSpawner.mu.Unlock()
			if _, err := r.ConcludeGame(ctx, gameID, 0.5, 30); err != nil {
				t.Fatal(err)
			}
		}
		promoter.mu.Lock()
		got := append([]*journal.Verdict(nil), promoter.verdicts...)
		promoter.mu.Unlock()
		if len(got) != 1 {
			t.Fatalf("promoter called %d times, want 1", len(got))
		}
		if got[0] == nil || got[0].Outcome != CohortOutcomePromote {
			t.Fatalf("promoter saw verdict %+v, want the final promote verdict", got[0])
		}
		if st, _ := r.Status(id); st != StatusDone {
			t.Errorf("status = %s, want done", st)
		}
	})

	t.Run("UC-043/BR-006 the concluded decision carries the final give-up verdict", func(t *testing.T) {
		spawner := &fakeSpawner{}
		r := newTestRegistry(t, RegistryConfig{
			MaxShadowGames: 4, Spawner: spawner, Verdict: alwaysInconclusive{}, Targeting: &fakeTargeting{},
		})
		id := startExperiment(t, r)
		drainToTerminal(t, r, spawner, id, 0.5, 200)

		evs := journalEvents(t, r, id)
		var final *journal.Verdict
		for _, ev := range evs {
			if ev.Kind == journal.KindDecision && ev.Status == string(StatusConcluded) {
				final = ev.Verdict
			}
		}
		if final == nil || final.Outcome != OutcomeInconclusive || final.Reason == "always inconclusive (test)" {
			t.Fatalf("concluded decision verdict = %+v, want the final give-up verdict", final)
		}
		view, _ := r.CompactView(id)
		if view.Verdict == nil || view.Verdict.Reason != final.Reason {
			t.Errorf("summary verdict = %+v, want the final verdict %+v", view.Verdict, final)
		}
	})
}
