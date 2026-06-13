package experiment

import (
	"context"
	"encoding/json"
	"testing"
)

// targeting_writer_test.go covers the #991 teardown additions: Writer.Strip (the
// un-nest + variant removal) and the GateTargetingWriter seam adapter the registry
// injects.

// flagVariants returns the variants map of a flag from the file at path.
func flagVariants(t *testing.T, path, flagKey string) map[string]json.RawMessage {
	t.Helper()
	raw := readFlagRaw(t, path, flagKey)
	var f struct {
		Variants map[string]json.RawMessage `json:"variants"`
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("decode flag %q: %v", flagKey, err)
	}
	return f.Variants
}

// flagTargeting returns the raw targeting block of a flag (nil when absent).
func flagTargeting(t *testing.T, path, flagKey string) json.RawMessage {
	t.Helper()
	raw := readFlagRaw(t, path, flagKey)
	var f struct {
		Targeting json.RawMessage `json:"targeting"`
	}
	if err := json.Unmarshal(raw, &f); err != nil {
		t.Fatalf("decode flag %q: %v", flagKey, err)
	}
	return f.Targeting
}

// TestStrip_RestoresPreExperimentState applies an experiment to a flag that had NO
// targeting, then strips it, and asserts the flag is back to its pre-experiment
// shape: no experiment variant, no targeting key.
func TestStrip_RestoresPreExperimentState(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	const flag = "invincibility_seconds" // numeric, no pre-existing targeting
	const expID = "exp_aaaa1111bbbb"

	beforeVariants := flagVariants(t, path, flag)
	if tg := flagTargeting(t, path, flag); len(tg) != 0 {
		t.Fatalf("fixture precondition: %q should have no targeting, got %s", flag, tg)
	}

	if err := w.Apply(Proposal{FlagKey: flag, ExperimentID: expID, ExperimentalValue: 6.0}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	// The experiment variant + a targeting branch now exist.
	if _, ok := flagVariants(t, path, flag)[experimentVariantName(expID)]; !ok {
		t.Fatalf("after apply: experiment variant %q missing", experimentVariantName(expID))
	}
	if len(flagTargeting(t, path, flag)) == 0 {
		t.Fatalf("after apply: targeting branch missing")
	}

	if err := w.Strip(flag, expID); err != nil {
		t.Fatalf("strip: %v", err)
	}

	// Variant gone; targeting key removed (the flag had none before).
	afterVariants := flagVariants(t, path, flag)
	if _, ok := afterVariants[experimentVariantName(expID)]; ok {
		t.Fatalf("after strip: experiment variant still present")
	}
	if len(afterVariants) != len(beforeVariants) {
		t.Fatalf("after strip: variant count %d != pre-experiment %d", len(afterVariants), len(beforeVariants))
	}
	if tg := flagTargeting(t, path, flag); len(tg) != 0 {
		t.Fatalf("after strip: targeting should be gone, got %s", tg)
	}
}

// TestStrip_LeavesOtherExperimentsAndPreExistingTargeting strips one experiment from
// a flag carrying ANOTHER experiment AND a game-authored targeting, and asserts only
// the named experiment's branch+variant are removed (the K-1 others survive, the
// real resolution is unchanged).
func TestStrip_LeavesOtherExperimentsAndPreExistingTargeting(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	const flag = "sensitivity" // has a pre-existing game_mode==Werewolf targeting
	const expA = "exp_aaaaaaaaaaaa"
	const expB = "exp_bbbbbbbbbbbb"

	// Real resolution (game_kind=real, no experiment_id) before any experiment.
	realBeforeVar, realBeforeVal := resolveVariant(t, path, flag, "real")

	if err := w.Apply(Proposal{FlagKey: flag, ExperimentID: expA, ExperimentalValue: 3}); err != nil {
		t.Fatalf("apply A: %v", err)
	}
	if err := w.Apply(Proposal{FlagKey: flag, ExperimentID: expB, ExperimentalValue: 1}); err != nil {
		t.Fatalf("apply B: %v", err)
	}

	if err := w.Strip(flag, expA); err != nil {
		t.Fatalf("strip A: %v", err)
	}

	variants := flagVariants(t, path, flag)
	if _, ok := variants[experimentVariantName(expA)]; ok {
		t.Fatalf("strip A: A's variant still present")
	}
	if _, ok := variants[experimentVariantName(expB)]; !ok {
		t.Fatalf("strip A: B's variant was wrongly removed")
	}

	// B's branch must still resolve for a B-experimental context.
	gotB, _ := resolveExperiment(t, path, flag, experimentalCtx(expB))
	if gotB != experimentVariantName(expB) {
		t.Fatalf("strip A: B's experimental arm no longer resolves (got %q)", gotB)
	}

	// The real resolution must be byte-identical to before any experiment (the
	// invariant teardown can only ever strengthen).
	realAfterVar, realAfterVal := resolveVariant(t, path, flag, "real")
	if realAfterVar != realBeforeVar {
		t.Fatalf("strip A: real variant changed %q -> %q", realBeforeVar, realAfterVar)
	}
	if !jsonEqual(mustJSON(t, realBeforeVal), mustJSON(t, realAfterVal)) {
		t.Fatalf("strip A: real value changed %v -> %v", realBeforeVal, realAfterVal)
	}
}

// TestStrip_Idempotent strips an experiment that was never applied (and one twice):
// both are no-ops, not errors.
func TestStrip_Idempotent(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())

	const flag = "invincibility_seconds"
	const expID = "exp_dddddddddddd"

	// Strip of a never-applied experiment is a SEMANTIC no-op: the target flag must
	// be byte-unchanged. (The whole-file bytes can differ — any write through the
	// Writer re-canonicalizes JSON formatting, exactly as Apply does — so we compare
	// the specific flag, semantically, not the raw file.)
	beforeFlag := readFlagRaw(t, path, flag)
	if err := w.Strip(flag, expID); err != nil {
		t.Fatalf("strip never-applied: %v", err)
	}
	afterFlag := readFlagRaw(t, path, flag)
	if !jsonEqual(beforeFlag, afterFlag) {
		t.Fatalf("strip of a never-applied experiment changed flag %q:\nbefore=%s\nafter=%s", flag, beforeFlag, afterFlag)
	}

	// Apply then strip twice.
	if err := w.Apply(Proposal{FlagKey: flag, ExperimentID: expID, ExperimentalValue: 5.0}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if err := w.Strip(flag, expID); err != nil {
		t.Fatalf("strip 1: %v", err)
	}
	if err := w.Strip(flag, expID); err != nil {
		t.Fatalf("strip 2 (idempotent): %v", err)
	}
}

// TestStrip_UnknownFlagIsNoOp confirms teardown does not fail when the flag the
// experiment scoped is gone from game.json.
func TestStrip_UnknownFlagIsNoOp(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())
	if err := w.Strip("flag_that_does_not_exist", "exp_zzzzzzzzzzzz"); err != nil {
		t.Fatalf("strip unknown flag should be a no-op, got: %v", err)
	}
}

// TestGateTargetingWriter_ApplyAndStrip exercises the registry seam adapter: Apply
// routes through the Gate (so a bad proposal is rejected) and Strip removes the
// branch via the Writer.
func TestGateTargetingWriter_ApplyAndStrip(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())
	g := NewGate(w, nil, discardLogger())
	tw := NewGateTargetingWriter(g, w)

	const flag = "invincibility_seconds"
	const expID = "exp_eeeeeeeeeeee"
	ctx := context.Background()

	if err := tw.Apply(ctx, Proposal{FlagKey: flag, ExperimentID: expID, ExperimentalValue: 7.0}); err != nil {
		t.Fatalf("adapter Apply: %v", err)
	}
	if _, ok := flagVariants(t, path, flag)[experimentVariantName(expID)]; !ok {
		t.Fatalf("adapter Apply: experiment variant missing")
	}

	if err := tw.Strip(ctx, flag, expID); err != nil {
		t.Fatalf("adapter Strip: %v", err)
	}
	if _, ok := flagVariants(t, path, flag)[experimentVariantName(expID)]; ok {
		t.Fatalf("adapter Strip: experiment variant still present")
	}
}

// TestGateTargetingWriter_ApplyRejectsInvariantViolation confirms the adapter's
// Apply is GATED: a mistyped value (string on a numeric flag) is rejected and
// nothing is written — the registry cannot bypass the invariant.
func TestGateTargetingWriter_ApplyRejectsInvariantViolation(t *testing.T) {
	path := newGameFile(t)
	w := NewWriter(path, discardLogger())
	g := NewGate(w, nil, discardLogger())
	tw := NewGateTargetingWriter(g, w)

	const flag = "invincibility_seconds" // numeric
	err := tw.Apply(context.Background(), Proposal{
		FlagKey: flag, ExperimentID: "exp_ffffffffffff", ExperimentalValue: "not-a-number",
	})
	if err == nil {
		t.Fatal("adapter Apply: expected a type-mismatch rejection, got nil")
	}
	if rej, ok := AsReject(err); !ok || rej.Reason != ReasonTypeMismatch {
		t.Fatalf("adapter Apply: expected ReasonTypeMismatch, got %v", err)
	}
	if _, ok := flagVariants(t, path, flag)[experimentVariantName("exp_ffffffffffff")]; ok {
		t.Fatal("adapter Apply: rejected proposal still wrote a variant")
	}
}

// mustJSON marshals v or fails the test.
func mustJSON(t *testing.T, v any) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return raw
}
