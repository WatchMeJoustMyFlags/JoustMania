package actions

import (
	"context"
	"os"
	"testing"

	"github.com/joustmania/agent/decision"
)

// parses reports whether the file at path is a valid interventions document.
func parses(t *testing.T, path string) bool {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %q: %v", path, err)
	}
	_, err = parseDoc(raw)
	return err == nil
}

// TestNeutralDocument_IsValid asserts the embedded self-heal target parses and
// carries every flag the writer drives, so a heal never produces a half-schema.
func TestNeutralDocument_IsValid(t *testing.T) {
	doc, err := parseDoc(NeutralDocument())
	if err != nil {
		t.Fatalf("embedded neutral document does not parse: %v", err)
	}
	for _, key := range []string{
		flagMusicTempoOverride, flagGlobalSensitivityOverride,
		flagPlayerSensitivityFactor, flagShieldSeconds, flagVolumeOverride,
		flagGlobalDifficultyFactor, flagPacingProfile, flagEliminatePlayer,
		flagRevivePlayer, flagAudioCue, flagControllerEffect, flagEndGame,
	} {
		if _, err := doc.flag(key); err != nil {
			t.Errorf("neutral document missing flag %q: %v", key, err)
		}
	}
}

// TestValidateAfterWrite_RestoresLastKnownGood is the core chaos case: the file
// on disk is poisoned (truncated) after a write, and validate-after-write
// restores the last-known-good bytes and increments the corruption counter
// (phase=write).
func TestValidateAfterWrite_RestoresLastKnownGood(t *testing.T) {
	w, path, reader := newMeteredWriter(t)

	// lastGood is the pristine fixture currently on disk (it parses).
	lastGood, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	// Simulate a crash/concurrent truncation between write and re-read.
	if err := os.WriteFile(path, []byte(`{"flags": `), 0o644); err != nil {
		t.Fatalf("truncate file: %v", err)
	}
	if parses(t, path) {
		t.Fatal("precondition: truncated file should not parse")
	}

	verr := w.validateAfterWrite(lastGood)
	if verr == nil {
		t.Fatal("validateAfterWrite returned nil, want corruption error")
	}
	if !parses(t, path) {
		t.Fatal("file still corrupt after validateAfterWrite; last-known-good not restored")
	}

	got := corruptionByPhase(t, reader)
	if got[phaseWrite] != 1 {
		t.Errorf("corruption{phase=write} = %d, want 1", got[phaseWrite])
	}
}

// TestApply_ProducesValidFile is the happy path: a normal Apply leaves the file
// parseable and records no corruption.
func TestApply_ProducesValidFile(t *testing.T) {
	w, path, reader := newMeteredWriter(t)

	if err := w.Apply(context.Background(), decision.Decision{
		Intervention: decision.InterventionAdjustMusicTempo,
	}); err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if !parses(t, path) {
		t.Fatal("file does not parse after a normal Apply")
	}
	if got := corruptionByPhase(t, reader); len(got) != 0 {
		t.Errorf("unexpected corruption recorded on happy path: %v", got)
	}
}

// TestHealIfCorrupt_TruncatedFile is the startup chaos case: the file is
// truncated before the agent starts; HealIfCorrupt restores a valid neutral
// document and increments the corruption counter (phase=startup).
func TestHealIfCorrupt_TruncatedFile(t *testing.T) {
	w, path, reader := newMeteredWriter(t)

	if err := os.WriteFile(path, []byte("{ this is not json"), 0o644); err != nil {
		t.Fatalf("truncate file: %v", err)
	}

	healed, err := w.HealIfCorrupt(context.Background())
	if err != nil {
		t.Fatalf("HealIfCorrupt: %v", err)
	}
	if !healed {
		t.Fatal("HealIfCorrupt reported healed=false, want true for corrupt file")
	}
	if !parses(t, path) {
		t.Fatal("file still corrupt after HealIfCorrupt")
	}
	if got := corruptionByPhase(t, reader); got[phaseStartup] != 1 {
		t.Errorf("corruption{phase=startup} = %d, want 1", got[phaseStartup])
	}
}

// TestHealIfCorrupt_ValidFileIsNoop asserts a valid file is left untouched and no
// corruption is recorded (the agent must not clobber operator/agent state).
func TestHealIfCorrupt_ValidFileIsNoop(t *testing.T) {
	w, path, reader := newMeteredWriter(t)

	before, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}

	healed, err := w.HealIfCorrupt(context.Background())
	if err != nil {
		t.Fatalf("HealIfCorrupt: %v", err)
	}
	if healed {
		t.Fatal("HealIfCorrupt healed a valid file; want no-op")
	}
	after, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read after: %v", err)
	}
	if string(before) != string(after) {
		t.Error("HealIfCorrupt mutated a valid file")
	}
	if got := corruptionByPhase(t, reader); len(got) != 0 {
		t.Errorf("unexpected corruption recorded on valid file: %v", got)
	}
}

// TestHealIfCorrupt_MissingFile heals a missing file to the neutral document
// (the bind mount could be empty on a fresh deploy).
func TestHealIfCorrupt_MissingFile(t *testing.T) {
	w, path, reader := newMeteredWriter(t)

	if err := os.Remove(path); err != nil {
		t.Fatalf("remove file: %v", err)
	}

	healed, err := w.HealIfCorrupt(context.Background())
	if err != nil {
		t.Fatalf("HealIfCorrupt: %v", err)
	}
	if !healed {
		t.Fatal("HealIfCorrupt did not heal a missing file")
	}
	if !parses(t, path) {
		t.Fatal("missing file not restored to a valid document")
	}
	if got := corruptionByPhase(t, reader); got[phaseStartup] != 1 {
		t.Errorf("corruption{phase=startup} = %d, want 1", got[phaseStartup])
	}
}
