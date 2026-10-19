package state

import (
	"strings"
	"testing"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
)

// UC: UC-053 A9
func TestSnapshotStamp_CrossSetConsistency(t *testing.T) {
	stamp := func(t *testing.T, f *fixture, key string) (any, any) {
		t.Helper()
		meta, _ := f.snapshotFlag(key)["metadata"].(map[string]any)
		return meta[MetaSnapshotEpoch], meta[MetaSnapshotVersion]
	}
	t.Run("UC-053/A9 interventions flags and the allow-list carry the current snapshot", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{epoch: "e7"})
		f.register("game_a", "c/1", "MOCK0001")
		f.put(WriteRequest{Set: "interventions", Flag: "volume_override", Value: raw("0.5"), Scope: Scope{GameID: "game_a"}})
		want := float64(f.state.Version()) // JSON numbers decode as float64
		for _, key := range []string{"volume_override", "end_game", "interventions_allowed"} {
			epoch, version := stamp(t, f, key)
			if epoch != "e7" || version != want {
				t.Fatalf("%s stamp = (%v, %v), want (e7, %v)", key, epoch, version, want)
			}
		}
	})
	t.Run("UC-053/A9 a write to one set moves the allow-list stamp too", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		_, before := stamp(t, f, "interventions_allowed")
		f.put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "slow", Writer: "test"})
		_, after := stamp(t, f, "interventions_allowed")
		if after == before {
			t.Fatalf("allow-list stamp stayed at %v after a write", after)
		}
	})
	t.Run("UC-053/A9 other agent flags stay unstamped", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		if epoch, version := stamp(t, f, "enabled"); epoch != nil || version != nil {
			t.Fatalf("agent/enabled stamped (%v, %v)", epoch, version)
		}
	})
}

// UC: UC-053 A9
func TestNew_RefusesInterventionsWithoutAgent(t *testing.T) {
	t.Run("UC-053/A9 interventions served without the agent allow-list is a startup error", func(t *testing.T) {
		base, err := baseline.Load(repoPath(t, "../flagd/ci"), baseline.DefaultFiles)
		if err != nil {
			t.Fatalf("baseline: %v", err)
		}
		reg, err := registry.LoadFile(repoPath(t, "registry.yaml"), base)
		if err != nil {
			t.Fatalf("registry: %v", err)
		}
		_, _, err = New(base, reg, Options{Epoch: "e1", Served: []string{"system", "interventions"}})
		if err == nil || !strings.Contains(err.Error(), `"agent"`) {
			t.Fatalf("New() error = %v, want a refusal naming the agent set", err)
		}
		if _, _, err := New(base, reg, Options{Epoch: "e1", Served: []string{"interventions", "agent"}}); err != nil {
			t.Fatalf("New() with both sets: %v", err)
		}
	})
}
