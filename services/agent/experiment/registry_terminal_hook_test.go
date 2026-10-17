package experiment

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
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
	t.Run("UC-042/BR-005 hook runs outside the registry lock", func(t *testing.T) {
		r := newTestRegistry(t, RegistryConfig{
			Spawner: &fakeSpawner{}, Verdict: fakeVerdict{concludeAtN: 1000}, Targeting: &fakeTargeting{},
			MaxShadowGames: 2,
		})
		// LiveCount takes the registry lock: if the hook ran under it, this deadlocks.
		r.SetTerminalHook(func(string) { _ = r.LiveCount() })
		id := startedExperiment(t, r)
		done := make(chan error, 1)
		go func() { done <- r.Abort(context.Background(), id, "kill-switch") }()
		select {
		case err := <-done:
			if err != nil {
				t.Fatalf("abort: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("terminal hook deadlocked: it runs under the registry lock")
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

// UC: UC-042 BR-006
func TestRegistryRecordPatternEnd_JournalHasNoSerial(t *testing.T) {
	r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2})
	id := startExperiment(t, r)
	a := testPin(t, "p").Archetypes[0]
	serials := []string{"MOCK0042", "00:06:f7:c9:a1:b2"}
	r.RecordPatternEnd(patterngame.Record{
		Info:       patterngame.Info{GameID: "game_1", ExperimentID: id, Arm: ArmControl, Archetype: a, Serials: serials},
		Outcome:    patterngame.OutcomeWinner,
		Slots:      []patterngame.SlotRecord{{Slot: 0, Serial: serials[0]}, {Slot: 1, Serial: serials[1]}},
		WinnerSlot: 1,
	})
	ended := eventsOfKind(journalEvents(t, r, id), journal.KindPatternGameEnded)
	if len(ended) != 1 {
		t.Fatalf("pattern_game_ended events = %d, want 1", len(ended))
	}
	raw, err := json.Marshal(ended[0])
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range serials {
		if strings.Contains(string(raw), s) {
			t.Errorf("UC-042/BR-006: journal event carries serial %q: %s", s, raw)
		}
	}
}
