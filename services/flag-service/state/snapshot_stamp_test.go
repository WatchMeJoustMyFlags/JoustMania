package state

import "testing"

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
