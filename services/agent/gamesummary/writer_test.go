package gamesummary

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func TestWriter_WritesFilePerGame(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)
	if err := w.Write(Summary{SchemaVersion: SchemaVersion, GameID: "game_one"}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if err := w.Write(Summary{SchemaVersion: SchemaVersion, GameID: "game_two"}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	for _, id := range []string{"game_one", "game_two"} {
		path := filepath.Join(dir, id+".json")
		if _, err := os.Stat(path); err != nil {
			t.Errorf("expected summary file %s: %v", path, err)
		}
	}
}

func TestWriter_CreatesDirIfMissing(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "summaries")
	w := NewWriter(dir)
	if err := w.Write(Summary{GameID: "game_x"}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, "game_x.json")); err != nil {
		t.Errorf("dir not created / file missing: %v", err)
	}
}

func TestWriter_ContentIsValidJSON(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)
	in := Summary{SchemaVersion: SchemaVersion, GameID: "game_json", GameKind: "shadow", PlayerCount: 4}
	if err := w.Write(in); err != nil {
		t.Fatalf("Write: %v", err)
	}
	data, err := os.ReadFile(w.Path("game_json"))
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var out Summary
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatalf("on-disk summary is not valid JSON: %v", err)
	}
	if out.GameID != "game_json" || out.GameKind != "shadow" || out.PlayerCount != 4 {
		t.Errorf("round-trip mismatch: %+v", out)
	}
}

// TestWriter_AtomicNoPartialFile: after a Write the destination is the COMPLETE
// file and no temp (.tmp) leftovers remain — a reader never sees a partial write.
func TestWriter_AtomicNoPartialFile(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)
	if err := w.Write(Summary{GameID: "game_atomic", PlayerCount: 2}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var json, tmp int
	for _, e := range entries {
		switch {
		case strings.HasSuffix(e.Name(), ".json"):
			json++
		case strings.HasSuffix(e.Name(), ".tmp"):
			tmp++
		}
	}
	if json != 1 {
		t.Errorf("want 1 .json file, got %d", json)
	}
	if tmp != 0 {
		t.Errorf("want 0 leftover .tmp files, got %d", tmp)
	}
}

// TestWriter_SanitizesGameID: an id with path-traversal / unsafe chars cannot
// escape the dir or produce an unwritable name.
func TestWriter_SanitizesGameID(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)
	if err := w.Write(Summary{GameID: "../../etc/evil"}); err != nil {
		t.Fatalf("Write: %v", err)
	}
	path := w.Path("../../etc/evil")
	if filepath.Dir(path) != dir {
		t.Errorf("sanitized path %q escaped dir %q", path, dir)
	}
	if _, err := os.Stat(path); err != nil {
		t.Errorf("sanitized file not written: %v", err)
	}
}

// TestWriter_ConcurrentWrites: two concurrent game-ends (#845) for distinct ids
// produce two independent files without collision.
func TestWriter_ConcurrentWrites(t *testing.T) {
	dir := t.TempDir()
	w := NewWriter(dir)
	var wg sync.WaitGroup
	for _, id := range []string{"game_a", "game_b", "game_c", "game_d"} {
		wg.Add(1)
		go func(id string) {
			defer wg.Done()
			if err := w.Write(Summary{GameID: id}); err != nil {
				t.Errorf("Write %s: %v", id, err)
			}
		}(id)
	}
	wg.Wait()
	entries, _ := os.ReadDir(dir)
	if len(entries) != 4 {
		t.Errorf("want 4 files, got %d: %v", len(entries), entries)
	}
}

func TestDirFromEnv(t *testing.T) {
	t.Setenv(gameSummaryDirEnv, "/custom/summaries")
	if got := DirFromEnv(); got != "/custom/summaries" {
		t.Errorf("DirFromEnv = %q, want /custom/summaries", got)
	}
	t.Setenv(gameSummaryDirEnv, "")
	if got := DirFromEnv(); got != DefaultDir {
		t.Errorf("DirFromEnv fallback = %q, want %q", got, DefaultDir)
	}
}
