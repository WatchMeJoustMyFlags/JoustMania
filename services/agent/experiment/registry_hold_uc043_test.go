package experiment

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
)

// holdPin is a two-archetype pin; burst lists which archetypes press the trigger.
func holdPin(t *testing.T, burst ...bool) patterngame.Pin {
	t.Helper()
	pin := testPin(t, "calm", "chaotic")
	for i, b := range burst {
		if b {
			pin.Archetypes[i].TriggerPolicy = patterngame.TriggerPolicy{Kind: patterngame.TriggerPolicyBurst}
		}
	}
	return pin
}

// switchGate is a HoldGate whose answer the test changes between ticks.
type switchGate struct {
	mu     sync.Mutex
	reason string
	err    error
	calls  int
}

func (g *switchGate) set(reason string, err error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.reason, g.err = reason, err
}

func (g *switchGate) gate(_ context.Context, _ string) (string, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.calls++
	return g.reason, g.err
}

func startHold(t *testing.T, r *Registry, flagKey string, value any) string {
	t.Helper()
	in := sampleIntent()
	in.FlagKey, in.ExperimentalValue = flagKey, value
	id, err := r.Declare(in)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Start(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	return id
}

func lastOutcomeNote(t *testing.T, r *Registry, id string) string {
	t.Helper()
	outcomes := eventsOfKind(journalEvents(t, r, id), journal.KindOutcome)
	if len(outcomes) == 0 {
		return ""
	}
	return outcomes[len(outcomes)-1].Note
}

// UC: UC-043 A17
func TestDeclareRefusesActivationAwayFromHold(t *testing.T) {
	r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: &patternSpawner{}})
	for _, v := range []any{"instant", "trigger", 1.0} {
		t.Run("UC-043/A17 shield.activation toward "+strings.TrimSpace(fmtAny(v))+" is refused", func(t *testing.T) {
			in := sampleIntent()
			in.FlagKey, in.ExperimentalValue = HoldActivationKey, v
			if _, err := r.Declare(in); err == nil {
				t.Fatal("declared")
			}
		})
	}
	t.Run("UC-043/A17 shield.activation toward hold is declared", func(t *testing.T) {
		in := sampleIntent()
		in.FlagKey, in.ExperimentalValue = HoldActivationKey, HoldValue
		if _, err := r.Declare(in); err != nil {
			t.Fatal(err)
		}
	})
}

func fmtAny(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return "a number"
}

// UC: UC-046 A17
func TestRegistryHoldPinDiscards(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-046/A17 an archetype without a trigger policy discards a hold experiment", func(t *testing.T) {
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{holdPin(t, true, false)}}})
		id := startHold(t, r, HoldActivationKey, HoldValue)
		if n := r.AllocateAndSpawn(ctx); n != 0 || spawner.count() != 0 {
			t.Fatalf("spawned %d", n)
		}
		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Fatalf("status = %s, want discarded", st)
		}
		if note := lastOutcomeNote(t, r, id); !strings.HasPrefix(note, patterngame.DiscardTriggerPolicyMissing) || !strings.Contains(note, "chaotic") {
			t.Fatalf("outcome note %q", note)
		}
	})
	t.Run("UC-046/A17 the same selection runs an experiment on another flag", func(t *testing.T) {
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{holdPin(t, true, false)}}})
		id := startExperiment(t, r)
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusRunning || spawner.count() == 0 {
			t.Fatalf("status = %s spawns %d, want running with spawns", st, spawner.count())
		}
	})
	t.Run("UC-043/A17 a hold sub-parameter under a non-hold activation is discarded at its pin", func(t *testing.T) {
		gate := &switchGate{reason: DiscardHoldNotRealDefault}
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner, HoldGate: gate.gate,
			Patterns: &queuePinner{pins: []patterngame.Pin{holdPin(t, true, true)}}})
		id := startHold(t, r, "shield.energy_budget_seconds", 20.0)
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusDiscarded || spawner.count() != 0 {
			t.Fatalf("status = %s spawns %d", st, spawner.count())
		}
		if note := lastOutcomeNote(t, r, id); !strings.HasPrefix(note, DiscardHoldNotRealDefault) {
			t.Fatalf("outcome note %q", note)
		}
	})
	t.Run("UC-043/A17 a read error leaves the experiment unpinned until the next tick", func(t *testing.T) {
		gate := &switchGate{err: errors.New("flag service down")}
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner, HoldGate: gate.gate,
			Patterns: &queuePinner{pins: []patterngame.Pin{holdPin(t, true, true)}}})
		id := startHold(t, r, "shield.energy_budget_seconds", 20.0)
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusRunning || spawner.count() != 0 {
			t.Fatalf("status = %s spawns %d, want running, nothing spawned", st, spawner.count())
		}
		gate.set("", nil)
		r.AllocateAndSpawn(ctx)
		if spawner.count() == 0 {
			t.Fatal("no spawn once the gate reads again")
		}
	})
}

// UC: UC-043 A17
func TestRegistryHoldStaleDiscard(t *testing.T) {
	ctx := context.Background()
	t.Run("UC-043/A17 a running sub-parameter experiment ends once activation leaves hold", func(t *testing.T) {
		gate := &switchGate{}
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner, HoldGate: gate.gate,
			Patterns: &queuePinner{pins: []patterngame.Pin{holdPin(t, true, true)}}})
		id := startHold(t, r, "shield.energy_budget_seconds", 20.0)
		other := startExperiment(t, r)
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("status = %s, want running", st)
		}
		gate.set("", errors.New("transient"))
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("a read error discarded it: %s", st)
		}
		gate.set(DiscardHoldNotRealDefault, nil)
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Fatalf("status = %s, want discarded", st)
		}
		if note := lastOutcomeNote(t, r, id); !strings.HasPrefix(note, DiscardHoldNotRealDefault) {
			t.Fatalf("outcome note %q", note)
		}
		if st, _ := r.Status(other); st != StatusRunning {
			t.Fatalf("the non-hold experiment = %s, want running", st)
		}
	})
}
