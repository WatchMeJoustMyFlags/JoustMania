package promote

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// realdefault_test.go exercises the autonomous REAL-default writer against a temp
// flagd JSON file (no flagd, no network) — the separate, higher-privilege write
// the safety rail isolates from the shadow Writer. Built here via NewGitClient's
// sibling NewRealDefaultWriter-equivalent: the struct is unexported and only
// FromEnv constructs it, so the test builds it through the env gate against a temp
// file (the env gate is set for THIS test only, via t.Setenv, restored after).

func writeFlagFile(t *testing.T, dir string) string {
	t.Helper()
	path := filepath.Join(dir, "game.json")
	doc := map[string]any{
		"metadata": map[string]any{"flagSetId": "game"},
		"flags": map[string]any{
			"death_grace_period_ms": map[string]any{
				"state":          "ENABLED",
				"variants":       map[string]any{"default": 300, "long": 600},
				"defaultVariant": "default",
			},
		},
	}
	raw, _ := json.MarshalIndent(doc, "", "  ")
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestRealDefaultWriterChangesDefaultVariant(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)

	// Build the real-default writer through the env gate (set for this test only).
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
	t.Setenv("AGENT_REAL_DEFAULT_FLAG_PATH", path)

	w, err := NewRealDefaultWriterFromEnv(discardLogger())
	if err != nil {
		t.Fatalf("NewRealDefaultWriterFromEnv: %v", err)
	}
	if w == nil {
		t.Fatal("writer is nil despite a satisfied env gate")
	}

	// Promote value 500 to the REAL default.
	if err := w.SetRealDefault(context.Background(), "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}

	// Re-read and assert the real default now resolves 500.
	raw, _ := os.ReadFile(path)
	var doc struct {
		Flags map[string]struct {
			Variants       map[string]json.RawMessage `json:"variants"`
			DefaultVariant string                     `json:"defaultVariant"`
		} `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	flag := doc.Flags["death_grace_period_ms"]
	defVal := flag.Variants[flag.DefaultVariant]
	if string(defVal) != "500" {
		t.Errorf("real default resolves %s, want 500", defVal)
	}
	// The pre-existing variants are preserved.
	if _, ok := flag.Variants["default"]; !ok {
		t.Error("pre-existing 'default' variant was dropped")
	}
	if _, ok := flag.Variants["long"]; !ok {
		t.Error("pre-existing 'long' variant was dropped")
	}
}

// TestRealDefaultWriterRejectsTypeMismatch: defense-in-depth (#936 review) — the
// real-default write refuses a value whose JSON kind differs from the flag's
// current default, so a mistyped promotion can't degrade the flag for live players
// (flagd would TYPE_MISMATCH and fall back). The file must be left untouched.
func TestRealDefaultWriterRejectsTypeMismatch(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir) // death_grace_period_ms has NUMERIC variants (300/600)
	before, _ := os.ReadFile(path)

	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
	t.Setenv("AGENT_REAL_DEFAULT_FLAG_PATH", path)
	w, err := NewRealDefaultWriterFromEnv(discardLogger())
	if err != nil || w == nil {
		t.Fatalf("writer build: err=%v nil=%v", err, w == nil)
	}

	// A STRING value on the numeric flag must be refused.
	if err := w.SetRealDefault(context.Background(), "death_grace_period_ms", "banana"); err == nil {
		t.Fatal("SetRealDefault accepted a string value on a numeric flag — must reject (type mismatch)")
	}
	// File unchanged — no partial/corrupting write.
	after, _ := os.ReadFile(path)
	if string(before) != string(after) {
		t.Error("flag file was modified despite the type-mismatch rejection")
	}
	// A numeric value (int or float) of the right kind is still accepted.
	if err := w.SetRealDefault(context.Background(), "death_grace_period_ms", 450); err != nil {
		t.Errorf("SetRealDefault rejected a same-kind numeric value: %v", err)
	}
}

// TestRealDefaultWriterRequiresAutonomousOptIn: even with the shared
// code-improvement gate on, the SECOND autonomous-specific opt-in is required.
func TestRealDefaultWriterRequiresAutonomousOptIn(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "") // the autonomous opt-in is OFF
	t.Setenv("AGENT_REAL_DEFAULT_FLAG_PATH", path)

	w, err := NewRealDefaultWriterFromEnv(discardLogger())
	if w != nil || err != nil {
		t.Errorf("writer built without AGENT_AUTONOMOUS_ENABLED: (%v, %v)", w, err)
	}
}
