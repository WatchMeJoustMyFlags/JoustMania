package experiment

import (
	"context"
	"sync"
	"testing"
)

// terminalRecorder records the ids the registry's terminal hook reports.
type terminalRecorder struct {
	mu  sync.Mutex
	ids []string
}

func (r *terminalRecorder) hook(id string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ids = append(r.ids, id)
}

func (r *terminalRecorder) got() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.ids...)
}

// UC: UC-042 BR-005
func TestRegistry_TerminalHookFiresOnConclusion(t *testing.T) {
	t.Run("UC-042/BR-005 conclude", func(t *testing.T) {
		spawner := &fakeSpawner{}
		r := newTestRegistry(t, RegistryConfig{
			Spawner:   spawner,
			Verdict:   fakeVerdict{outcome: CohortOutcomeDiscard, concludeAtN: 1, alwaysReport: true},
			Targeting: &fakeTargeting{}, MaxShadowGames: 2, EffectiveConcurrency: 1,
		})
		rec := &terminalRecorder{}
		r.SetTerminalHook(rec.hook)
		id := startedExperiment(t, r)
		r.AllocateAndSpawn(context.Background())
		if got := rec.got(); len(got) != 0 {
			t.Fatalf("hook fired before conclusion: %v", got)
		}
		if _, err := r.ConcludeGame(context.Background(), spawner.calls[0].gameID, 0.3, 60); err != nil {
			t.Fatalf("conclude: %v", err)
		}
		if got := rec.got(); len(got) == 0 || got[0] != id {
			t.Errorf("terminal hook ids = %v, want [%s]", got, id)
		}
	})
	t.Run("UC-042/BR-005 abort", func(t *testing.T) {
		r := newTestRegistry(t, RegistryConfig{
			Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
			MaxShadowGames: 2,
		})
		rec := &terminalRecorder{}
		r.SetTerminalHook(rec.hook)
		id := startedExperiment(t, r)
		if err := r.Abort(context.Background(), id, "kill-switch"); err != nil {
			t.Fatalf("abort: %v", err)
		}
		if got := rec.got(); len(got) == 0 || got[0] != id {
			t.Errorf("terminal hook ids = %v, want [%s]", got, id)
		}
	})
}
