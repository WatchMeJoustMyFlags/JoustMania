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
	t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir()) // durable snapshot store (#1076) → temp

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
	t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir()) // durable snapshot store (#1076) → temp
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

// resolvedDefault reads back the value the flag's defaultVariant currently resolves
// to (the value real games would see). "" means the flag has no defaultVariant.
func resolvedDefault(t *testing.T, path, flagKey string) string {
	t.Helper()
	raw, _ := os.ReadFile(path)
	var doc struct {
		Flags map[string]struct {
			Variants       map[string]json.RawMessage `json:"variants"`
			DefaultVariant *string                    `json:"defaultVariant"`
		} `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	flag := doc.Flags[flagKey]
	if flag.DefaultVariant == nil {
		return ""
	}
	return string(flag.Variants[*flag.DefaultVariant])
}

func realDefaultWriterForTest(t *testing.T, path string) *fileRealDefaultWriter {
	t.Helper()
	// Point the durable snapshot store (#1076) at a temp dir so tests never touch the
	// real /var/lib persistence path.
	return realDefaultWriterForTestWithStore(t, path, t.TempDir())
}

// TestRevertRestoresExactPriorValue is the safety-critical property (#1065): a
// revert restores the EXACT pre-promotion {defaultVariant name + value}, NOT merely a
// remembered variant name. The flag starts at default=300; we promote to 500, then
// revert, and the real default must resolve 300 again via the original "default"
// variant.
func TestRevertRestoresExactPriorValue(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir) // default=300, long=600, defaultVariant=default
	w := realDefaultWriterForTest(t, path)
	ctx := context.Background()

	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Fatalf("pre-promotion default = %s, want 300", got)
	}
	if err := w.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "500" {
		t.Fatalf("post-promotion default = %s, want 500", got)
	}
	if err := w.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("RevertRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Errorf("post-revert default = %s, want exact prior value 300", got)
	}
}

// TestRevertRestoresValueDespiteInterveningSlotOverwrite proves WHY snapshotting the
// VALUE (not just the name) is the safe design (#1065): an intervening write that
// CLOBBERS the original default variant's value must not corrupt the revert. We
// promote to 500 (snapshot captures {variant=default, value=300}), then an external
// edit overwrites the "default" variant's value to a garbage 999 in the file, then we
// revert. A name-only reverter would restore 999 (whatever the slot now holds); the
// value-snapshotting reverter restores the EXACT prior 300.
func TestRevertRestoresValueDespiteInterveningSlotOverwrite(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	w := realDefaultWriterForTest(t, path)
	ctx := context.Background()

	if err := w.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	// Simulate an intervening write clobbering the ORIGINAL default variant's value.
	clobberVariantValue(t, path, "death_grace_period_ms", "default", 999)
	if err := w.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("RevertRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Errorf("post-revert default = %s, want exact snapshotted prior value 300 (NOT the clobbered 999)", got)
	}
}

// clobberVariantValue rewrites one variant's value in the flag file in place,
// simulating an external/second writer that overwrote the slot.
func clobberVariantValue(t *testing.T, path, flagKey, variant string, value any) {
	t.Helper()
	raw, _ := os.ReadFile(path)
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("clobber re-parse: %v", err)
	}
	flags := doc["flags"].(map[string]any)
	flag := flags[flagKey].(map[string]any)
	variants := flag["variants"].(map[string]any)
	variants[variant] = value
	out, _ := json.MarshalIndent(doc, "", "  ")
	if err := os.WriteFile(path, out, 0o644); err != nil {
		t.Fatalf("clobber write: %v", err)
	}
}

// TestRevertToFirstSnapshotAcrossRepromotion: a second promotion of the SAME flag
// must NOT re-snapshot — a revert always rolls back to the state BEFORE the agent
// first touched the flag, never to an intermediate agent-written value (#1065).
func TestRevertToFirstSnapshotAcrossRepromotion(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir) // default=300
	w := realDefaultWriterForTest(t, path)
	ctx := context.Background()

	if err := w.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("first SetRealDefault: %v", err)
	}
	if err := w.SetRealDefault(ctx, "death_grace_period_ms", 600); err != nil {
		t.Fatalf("second SetRealDefault: %v", err)
	}
	if err := w.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("RevertRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Errorf("post-revert default = %s, want the original 300 (not the intermediate 500)", got)
	}
}

// TestRevertWithoutSnapshotIsRefused: a revert with no captured prior state must be
// refused rather than fabricating one (the unsafe behavior #1065 calls out).
func TestRevertWithoutSnapshotIsRefused(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	w := realDefaultWriterForTest(t, path)
	if err := w.RevertRealDefault(context.Background(), "death_grace_period_ms"); err == nil {
		t.Fatal("RevertRealDefault with no snapshot should be refused, got nil error")
	}
}

// realDefaultWriterForTestWithStore builds a writer like realDefaultWriterForTest but
// also points the durable snapshot store (#1076) at storeDir, so the test can build a
// SECOND writer over the same dir to simulate an agent restart. Returns the writer.
func realDefaultWriterForTestWithStore(t *testing.T, path, storeDir string) *fileRealDefaultWriter {
	t.Helper()
	t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
	t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
	t.Setenv("AGENT_REAL_DEFAULT_FLAG_PATH", path)
	t.Setenv("AGENT_EXPERIMENT_DIR", storeDir)
	w, err := NewRealDefaultWriterFromEnv(discardLogger())
	if err != nil || w == nil {
		t.Fatalf("writer build: err=%v nil=%v", err, w == nil)
	}
	return w
}

// TestSnapshotSurvivesRestartAndReverts is the #1076 key test: a snapshot persisted by
// one writer must let a SUBSEQUENT writer (a new instance over the SAME durable store,
// modeling an agent restart between apply and revert) revert to the EXACT pre-promotion
// value. Without persistence the second writer would find no snapshot and refuse the
// revert, stranding the regression on live players.
func TestSnapshotSurvivesRestartAndReverts(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir) // default=300, defaultVariant=default
	storeDir := t.TempDir()
	ctx := context.Background()

	// --- "before restart" writer: apply the promotion (captures + persists snapshot).
	w1 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if err := w1.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "500" {
		t.Fatalf("post-promotion default = %s, want 500", got)
	}

	// --- simulate restart: a brand-new writer over the SAME durable store dir. It must
	// rehydrate the snapshot from disk (w1's in-memory map is gone).
	w2 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if _, held := w2.snapshots["death_grace_period_ms"]; !held {
		t.Fatal("restart writer did not rehydrate the persisted snapshot (#1076 strand-on-restart bug)")
	}
	if err := w2.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("post-restart RevertRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Errorf("post-restart revert default = %s, want exact pre-promotion 300", got)
	}
}

// TestSnapshotSurvivesRestartDespiteInterveningClobber mirrors the #1065
// intervening-slot-overwrite test but across a RESTART boundary: the persisted
// snapshot must carry the VALUE (not just the variant name), so a post-restart revert
// restores the exact prior value even if the original variant's slot was clobbered.
func TestSnapshotSurvivesRestartDespiteInterveningClobber(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	storeDir := t.TempDir()
	ctx := context.Background()

	w1 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if err := w1.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	// An intervening write clobbers the ORIGINAL "default" variant's value to garbage.
	clobberVariantValue(t, path, "death_grace_period_ms", "default", 999)

	// Restart: new writer, must restore the SNAPSHOTTED 300, not the clobbered 999.
	w2 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if err := w2.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("post-restart RevertRealDefault: %v", err)
	}
	if got := resolvedDefault(t, path, "death_grace_period_ms"); got != "300" {
		t.Errorf("post-restart revert default = %s, want exact snapshotted 300 (NOT clobbered 999)", got)
	}
}

// TestRevertDropsPersistedSnapshotButKeepsCooldown: after a successful revert the
// durable snapshot is dropped (a fresh promotion re-snapshots), but the post-revert
// cooldown stamp is PERSISTED so thrash protection survives a restart. A restart after
// the revert must therefore NOT resurrect a stale snapshot, yet must expose the revert
// timestamp.
func TestRevertDropsPersistedSnapshotButKeepsCooldown(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	storeDir := t.TempDir()
	ctx := context.Background()

	w1 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if err := w1.SetRealDefault(ctx, "death_grace_period_ms", 500); err != nil {
		t.Fatalf("SetRealDefault: %v", err)
	}
	if err := w1.RevertRealDefault(ctx, "death_grace_period_ms"); err != nil {
		t.Fatalf("RevertRealDefault: %v", err)
	}

	// Restart: the stale snapshot must NOT come back, but the cooldown stamp must.
	w2 := realDefaultWriterForTestWithStore(t, path, storeDir)
	if _, held := w2.snapshots["death_grace_period_ms"]; held {
		t.Error("restart resurrected a snapshot for an already-reverted flag (would mis-revert a future un-snapshotted state)")
	}
	times := w2.PersistedRevertTimes()
	if _, ok := times["death_grace_period_ms"]; !ok {
		t.Error("post-revert cooldown stamp did not survive restart (#1076 item 3)")
	}
	// A revert with no snapshot is correctly refused after restart.
	if err := w2.RevertRealDefault(ctx, "death_grace_period_ms"); err == nil {
		t.Error("revert of an already-reverted flag after restart should be refused")
	}
}

// TestPromoteFailsClosedWhenSnapshotCannotPersist: if the durable snapshot write
// fails, SetRealDefault must REFUSE the promotion rather than mutate the flag file
// without a durable revert path (which would reintroduce the strand-on-restart bug).
func TestPromoteFailsClosedWhenSnapshotCannotPersist(t *testing.T) {
	dir := t.TempDir()
	path := writeFlagFile(t, dir)
	before, _ := os.ReadFile(path)
	storeDir := t.TempDir()
	ctx := context.Background()

	w := realDefaultWriterForTestWithStore(t, path, storeDir)
	// Make the store path unwritable: replace the store DIR with a regular file so the
	// MkdirAll/CreateTemp inside flush fails.
	if err := os.RemoveAll(storeDir); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(storeDir, []byte("not a dir"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := w.SetRealDefault(ctx, "death_grace_period_ms", 500); err == nil {
		t.Fatal("SetRealDefault must fail closed when the snapshot cannot be persisted")
	}
	// The flag file must be UNCHANGED — no real-default mutation without a durable revert.
	after, _ := os.ReadFile(path)
	if string(before) != string(after) {
		t.Error("flag file was mutated despite the snapshot-persist failure (strand-on-restart risk)")
	}
	// The in-memory snapshot must not linger (it would imply a revert is possible when
	// the file was never mutated).
	if _, held := w.snapshots["death_grace_period_ms"]; held {
		t.Error("in-memory snapshot lingered after a failed persist")
	}
}

// TestWriterIsAlsoReverter: the concrete writer satisfies RealDefaultReverter so the
// SAME instance backs both seams (it must, to share the at-write-time snapshot).
func TestWriterIsAlsoReverter(t *testing.T) {
	var _ RealDefaultReverter = (*fileRealDefaultWriter)(nil)
	var _ RealDefaultWriter = (*fileRealDefaultWriter)(nil)
}
