package state

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/flag-service/fserr"
)

// UC: UC-053 A3
// NFR: NFR-051
func TestExpireExperiments_PerExperiment(t *testing.T) {
	t.Run("UC-053/A3 an experiment is dropped from every flag together, never flag by flag (#2369 B3)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.put(WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: raw("4"), Scope: Scope{ExperimentID: "exp_two", Arm: "experimental"}})
		f.clock.Advance(DefaultExperimentIdle - time.Minute)
		// A later write on a second flag of the same experiment keeps the whole experiment alive.
		f.put(WriteRequest{Set: "game", Flag: "death_grace_period_seconds", Value: raw("2"), Scope: Scope{ExperimentID: "exp_two", Arm: "experimental"}})
		f.clock.Advance(2 * time.Minute)
		if n := f.state.ExpireExperiments(); n != 0 {
			t.Fatalf("expired %d experiments, want 0 (the second flag was written %v ago)", n, 2*time.Minute)
		}
		payload, _, _ := f.state.Snapshot("")
		if !containsStr(payload, "exp_two") {
			t.Fatal("the experiment lost its arm on the older flag")
		}
		f.clock.Advance(DefaultExperimentIdle)
		if n := f.state.ExpireExperiments(); n != 1 {
			t.Fatalf("expired %d, want 1 experiment", n)
		}
		payload, _, _ = f.state.Snapshot("")
		if containsStr(payload, "exp_two") {
			t.Fatal("the idle experiment kept an arm on one flag")
		}
	})
}

// UC: UC-053 A1
func TestRegisterGame_ExperimentFieldsBounded(t *testing.T) {
	t.Run("UC-053/A1 experiment_id and arm are bounded like the other ids (#2369)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		long := strings.Repeat("x", MaxIDLength+1)
		_, err := f.state.RegisterGame(Game{ID: "g1", CoordinatorInstance: "c/1", Kind: "shadow", ExperimentID: long, Arm: "experimental"})
		wantReason(t, err, 400, fserr.Malformed)
		_, err = f.state.RegisterGame(Game{ID: "g1", CoordinatorInstance: "c/1", Kind: "shadow", ExperimentID: "e", Arm: long})
		wantReason(t, err, 400, fserr.Malformed)
		_, err = f.state.RegisterGame(Game{ID: "g1", CoordinatorInstance: "c/1", Kind: "shadow", ExperimentID: strings.Repeat("x", MaxIDLength), Arm: "experimental"})
		if err != nil {
			t.Fatalf("an id at the bound was refused: %v", err)
		}
	})
}

// UC: UC-053 A10
func TestReconcile_NeverRehomesAnotherInstancesGame(t *testing.T) {
	t.Run("UC-053/A10 a listed id another instance holds refuses the whole batch (#2369)", func(t *testing.T) {
		f := newFixture(t, fixtureOpts{})
		f.register("game_a", "coordA/1", "S1")
		f.register("game_b", "coordB/1", "S2")
		_, err := f.state.Reconcile("coordA/1", []Game{{ID: "game_new", Kind: "real"}, {ID: "game_b", Kind: "real", Serials: []string{"S9"}}})
		wantReason(t, err, 409, fserr.GameIDTaken)
		if g := f.state.Game("game_b"); g == nil || g.CoordinatorInstance != "coordB/1" || len(g.Serials) != 1 || g.Serials[0] != "S2" {
			t.Fatalf("game_b was re-homed or changed: %+v", g)
		}
		if f.state.Game("game_new") != nil {
			t.Fatal("the refused batch registered game_new")
		}
		if g := f.state.Game("game_a"); g == nil {
			t.Fatal("the refused batch dropped the instance's own game")
		}
	})
}

// UC: UC-053 A12
func TestStateFile_FutureVersionIsKept(t *testing.T) {
	t.Run("UC-053/A12 a state file from a newer version is kept untouched and never written over (#2369)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		doc := `{"version":99,"values":[{"flag_set":"game","flag":"sensitivity","variant":"slow","written_at":"2026-10-16T12:00:00Z"}]}`
		if err := os.WriteFile(path, []byte(doc), 0o600); err != nil {
			t.Fatal(err)
		}
		f := newFixture(t, fixtureOpts{stateFile: path})
		got, err := os.ReadFile(path)
		if err != nil || string(got) != doc {
			t.Fatalf("the newer-version file was moved or changed: %v %q", err, got)
		}
		if m, _ := filepath.Glob(path + ".corrupt-*"); len(m) != 0 {
			t.Fatalf("the file was copied aside as corrupt: %v", m)
		}
		// The newer file's values are not loaded.
		if v := f.snapshotFlag("sensitivity")["defaultVariant"]; v == "slow" {
			t.Fatalf("sensitivity=slow of the newer file was loaded")
		}
		before := f.snapshotFlag("sensitivity")["defaultVariant"]
		// An ordinary flag: refused with persist_failed, memory unchanged.
		_, werr := f.state.Put(WriteRequest{Set: "game", Flag: "sensitivity", Variant: "fast", Persist: true, Writer: "menu"})
		wantReason(t, werr, 500, fserr.ReasonPersistFailed)
		if v := f.snapshotFlag("sensitivity")["defaultVariant"]; v != before {
			t.Fatalf("sensitivity changed to %v although the write was refused", v)
		}
		// A kill switch applies in memory and reports not persisted.
		res, werr := f.state.Put(WriteRequest{Set: "agent", Flag: "interventions_allowed", Variant: "full", Persist: true, Writer: "menu"})
		if werr != nil || res.Persisted {
			t.Fatalf("kill switch write: err=%v persisted=%v, want applied with persisted:false", werr, res.Persisted)
		}
		if v := f.snapshotFlag("interventions_allowed")["defaultVariant"]; v != "full" {
			t.Fatalf("the kill switch was not applied in memory: %v", v)
		}
		got, _ = os.ReadFile(path)
		if string(got) != doc {
			t.Fatalf("the newer-version file was overwritten: %q", got)
		}
	})
}
