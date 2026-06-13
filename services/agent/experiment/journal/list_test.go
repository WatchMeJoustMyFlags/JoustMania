package journal

import (
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"
)

// TestList_EnumeratesExperimentsWithIntent confirms List returns exactly the dirs
// holding an intent.json (the rehydration scan, #991/#978).
func TestList_EnumeratesExperimentsWithIntent(t *testing.T) {
	root := t.TempDir()

	mk := func(id string) {
		if _, err := Create(root, Intent{
			ExperimentID: id,
			CreatedAt:    time.Now().UTC(),
			FlagKey:      "sensitivity",
			Arms:         []string{"experimental", "control"},
		}); err != nil {
			t.Fatalf("create %s: %v", id, err)
		}
	}
	mk("exp_aaaa00000000")
	mk("exp_bbbb11111111")

	// A stray directory without an intent.json must be ignored.
	if err := os.MkdirAll(filepath.Join(root, "not_an_experiment"), 0o755); err != nil {
		t.Fatalf("mkdir stray: %v", err)
	}

	ids, err := List(root)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	sort.Strings(ids)
	want := []string{"exp_aaaa00000000", "exp_bbbb11111111"}
	if len(ids) != len(want) {
		t.Fatalf("List returned %v, want %v", ids, want)
	}
	for i := range want {
		if ids[i] != want[i] {
			t.Fatalf("List[%d]=%q want %q (full: %v)", i, ids[i], want[i], ids)
		}
	}
}

// TestList_MissingRootIsEmpty confirms a never-written root yields no ids and no
// error (a first-run agent rehydrates trivially).
func TestList_MissingRootIsEmpty(t *testing.T) {
	ids, err := List(filepath.Join(t.TempDir(), "does-not-exist"))
	if err != nil {
		t.Fatalf("List of missing root errored: %v", err)
	}
	if len(ids) != 0 {
		t.Fatalf("List of missing root returned %v, want empty", ids)
	}
}
