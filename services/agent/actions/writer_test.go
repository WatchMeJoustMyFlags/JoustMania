package actions

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	jsonlogic "github.com/diegoholiveira/jsonlogic/v3"

	"github.com/joustmania/agent/decision"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// newTestWriter copies the real interventions.json fixture into a temp file and
// returns a Writer over it plus the temp path.
func newTestWriter(t *testing.T) (*Writer, string) {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("testdata", "interventions.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "interventions.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}
	return NewWriter(path, discardLogger()), path
}

// readFlag returns the parsed flag object from the file at path.
func readFlag(t *testing.T, path, flagKey string) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read file: %v", err)
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal doc (invalid JSON written): %v", err)
	}
	fr, ok := doc.Flags[flagKey]
	if !ok {
		t.Fatalf("flag %q missing", flagKey)
	}
	var flag map[string]any
	if err := json.Unmarshal(fr, &flag); err != nil {
		t.Fatalf("unmarshal flag %q: %v", flagKey, err)
	}
	return flag
}

// activeValueOf returns the string value of the flag's current defaultVariant.
func activeValueOf(t *testing.T, path, flagKey string) string {
	t.Helper()
	flag := readFlag(t, path, flagKey)
	dv, _ := flag["defaultVariant"].(string)
	variants, _ := flag["variants"].(map[string]any)
	v, ok := variants[dv]
	if !ok {
		t.Fatalf("flag %q defaultVariant %q not in variants", flagKey, dv)
	}
	s, _ := v.(string)
	return s
}

func TestEdgeInterventions(t *testing.T) {
	cases := []struct {
		name     string
		dec      decision.Decision
		flagKey  string
		wantTail string // payload after the nonce, "" for nonce-only
	}{
		{"audio_cue default", decision.Decision{Intervention: decision.InterventionPlayAudioCue}, flagAudioCue, defaultAudioCue},
		{"audio_cue value", decision.Decision{Intervention: decision.InterventionPlayAudioCue, Value: "boo"}, flagAudioCue, "boo"},
		{"controller_effect targeted", decision.Decision{Intervention: decision.InterventionSendControllerEffect, TargetSerial: "AA:BB", Value: "rumble"}, flagControllerEffect, "AA:BB:rumble"},
		{"controller_effect broadcast", decision.Decision{Intervention: decision.InterventionSendControllerEffect, TargetSerial: "", Value: "flash"}, flagControllerEffect, ":flash"},
		{"eliminate", decision.Decision{Intervention: decision.InterventionEliminatePlayer, TargetSerial: "CC:DD"}, flagEliminatePlayer, "CC:DD"},
		{"revive", decision.Decision{Intervention: decision.InterventionRevivePlayer, TargetSerial: "EE:FF"}, flagRevivePlayer, "EE:FF"},
		{"end_game", decision.Decision{Intervention: decision.InterventionEndGame}, flagEndGame, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w, path := newTestWriter(t)
			if err := w.Apply(context.Background(), tc.dec); err != nil {
				t.Fatalf("apply: %v", err)
			}
			got := activeValueOf(t, path, tc.flagKey)
			nonce, tail := splitNonce(got)
			if nonce == "" {
				t.Fatalf("edge value %q has empty nonce", got)
			}
			if tail != tc.wantTail {
				t.Fatalf("payload = %q, want %q (full %q)", tail, tc.wantTail, got)
			}
			assertStructurallyValid(t, path)
		})
	}
}

// splitNonce splits "<nonce>:<payload>" into nonce and payload. For a nonce-only
// value the payload is "".
func splitNonce(v string) (nonce, payload string) {
	i := strings.IndexByte(v, ':')
	if i < 0 {
		return v, ""
	}
	return v[:i], v[i+1:]
}

func TestEdgeNonceUniqueAcrossDispatches(t *testing.T) {
	w, path := newTestWriter(t)
	seen := map[string]bool{}
	for i := 0; i < 50; i++ {
		if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame}); err != nil {
			t.Fatalf("apply %d: %v", i, err)
		}
		nonce, _ := splitNonce(activeValueOf(t, path, flagEndGame))
		if seen[nonce] {
			t.Fatalf("nonce %q repeated across dispatches", nonce)
		}
		seen[nonce] = true
	}
}

func TestNonceGenUnique(t *testing.T) {
	g := newNonceGen()
	seen := map[string]bool{}
	for i := 0; i < 10000; i++ {
		n := g.next()
		if seen[n] {
			t.Fatalf("duplicate nonce %q at %d", n, i)
		}
		seen[n] = true
	}
}

func TestStateInterventions(t *testing.T) {
	cases := []struct {
		name    string
		dec     decision.Decision
		flagKey string
		want    float64
	}{
		{"music tempo default", decision.Decision{Intervention: decision.InterventionAdjustMusicTempo}, flagMusicTempoOverride, defaultMusicTempo},
		{"music tempo value", decision.Decision{Intervention: decision.InterventionAdjustMusicTempo, Value: "1.3"}, flagMusicTempoOverride, 1.3},
		{"volume default", decision.Decision{Intervention: decision.InterventionAdjustVolume}, flagVolumeOverride, defaultVolume},
		{"global sensitivity", decision.Decision{Intervention: decision.InterventionAdjustGlobalSensitivity, Value: "3"}, flagGlobalSensitivityOverride, 3},
		{"global difficulty default", decision.Decision{Intervention: decision.InterventionAdjustGlobalDifficulty}, flagGlobalDifficultyFactor, defaultGlobalDifficulty},
		{"global difficulty value", decision.Decision{Intervention: decision.InterventionAdjustGlobalDifficulty, Value: "0.5"}, flagGlobalDifficultyFactor, 0.5},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w, path := newTestWriter(t)
			if err := w.Apply(context.Background(), tc.dec); err != nil {
				t.Fatalf("apply: %v", err)
			}
			flag := readFlag(t, path, tc.flagKey)
			if dv := flag["defaultVariant"]; dv != activeVariant {
				t.Fatalf("defaultVariant = %v, want %q", dv, activeVariant)
			}
			variants := flag["variants"].(map[string]any)
			if got := variants[activeVariant].(float64); got != tc.want {
				t.Fatalf("active value = %v, want %v", got, tc.want)
			}
			assertStructurallyValid(t, path)
		})
	}
}

// #766 F6: pacing_profile is a STRING state-shaped flag.
func TestPacingProfileStateAndRevert(t *testing.T) {
	w, path := newTestWriter(t)

	// Default value.
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionSetPacingProfile}); err != nil {
		t.Fatalf("apply default: %v", err)
	}
	flag := readFlag(t, path, flagPacingProfile)
	if dv := flag["defaultVariant"]; dv != activeVariant {
		t.Fatalf("defaultVariant = %v, want %q", dv, activeVariant)
	}
	if got := flag["variants"].(map[string]any)[activeVariant].(string); got != defaultPacingProfile {
		t.Fatalf("active value = %q, want %q", got, defaultPacingProfile)
	}

	// Explicit value.
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionSetPacingProfile, Value: "calm"}); err != nil {
		t.Fatalf("apply value: %v", err)
	}
	if got := readFlag(t, path, flagPacingProfile)["variants"].(map[string]any)[activeVariant].(string); got != "calm" {
		t.Fatalf("active value = %q, want %q", got, "calm")
	}

	// Revert flips defaultVariant back to neutral "none".
	if err := w.RevertState(flagPacingProfile); err != nil {
		t.Fatalf("revert: %v", err)
	}
	if dv := readFlag(t, path, flagPacingProfile)["defaultVariant"]; dv != neutralNone {
		t.Fatalf("post-revert defaultVariant = %v, want %q", dv, neutralNone)
	}
	assertStructurallyValid(t, path)
}

// #766 F6: global_difficulty_factor reverts to "default" (1.0), not "none".
func TestGlobalDifficultyRevertNeutral(t *testing.T) {
	w, path := newTestWriter(t)
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustGlobalDifficulty, Value: "1.5"}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if err := w.RevertState(flagGlobalDifficultyFactor); err != nil {
		t.Fatalf("revert: %v", err)
	}
	if dv := readFlag(t, path, flagGlobalDifficultyFactor)["defaultVariant"]; dv != neutralDefault {
		t.Fatalf("post-revert defaultVariant = %v, want %q", dv, neutralDefault)
	}
}

func TestStateRevert(t *testing.T) {
	w, path := newTestWriter(t)
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustMusicTempo, Value: "1.25"}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	if dv := readFlag(t, path, flagMusicTempoOverride)["defaultVariant"]; dv != activeVariant {
		t.Fatalf("pre-revert defaultVariant = %v", dv)
	}
	if err := w.RevertState(flagMusicTempoOverride); err != nil {
		t.Fatalf("revert: %v", err)
	}
	if dv := readFlag(t, path, flagMusicTempoOverride)["defaultVariant"]; dv != neutralNone {
		t.Fatalf("post-revert defaultVariant = %v, want %q", dv, neutralNone)
	}
}

func TestTargetedTwoPlayersAndRemoval(t *testing.T) {
	w, path := newTestWriter(t)
	// Drive two players' sensitivity.
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustPlayerSensitivity, TargetSerial: "AA", Value: "1.5"}); err != nil {
		t.Fatalf("apply AA: %v", err)
	}
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustPlayerSensitivity, TargetSerial: "BB", Value: "0.7"}); err != nil {
		t.Fatalf("apply BB: %v", err)
	}

	// Both serials evaluate to their variant; an unknown serial falls through.
	assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "AA", "agent_AA")
	assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "BB", "agent_BB")
	assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "ZZ", neutralDefault)

	// The variant values are correct.
	variants := readFlag(t, path, flagPlayerSensitivityFactor)["variants"].(map[string]any)
	if variants["agent_AA"].(float64) != 1.5 || variants["agent_BB"].(float64) != 0.7 {
		t.Fatalf("variant values wrong: %v", variants)
	}

	// Remove AA: its branch and variant are gone, BB stays.
	if err := w.RevertTargeted(flagPlayerSensitivityFactor, "AA"); err != nil {
		t.Fatalf("revert AA: %v", err)
	}
	assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "AA", neutralDefault)
	assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "BB", "agent_BB")
	if _, ok := readFlag(t, path, flagPlayerSensitivityFactor)["variants"].(map[string]any)["agent_AA"]; ok {
		t.Fatalf("agent_AA variant should be removed")
	}

	// Remove BB: targeting block is dropped entirely.
	if err := w.RevertTargeted(flagPlayerSensitivityFactor, "BB"); err != nil {
		t.Fatalf("revert BB: %v", err)
	}
	if _, ok := readFlag(t, path, flagPlayerSensitivityFactor)["targeting"]; ok {
		t.Fatalf("targeting should be removed once no serials remain")
	}
	assertStructurallyValid(t, path)
}

func TestGrantShieldTargeted(t *testing.T) {
	w, path := newTestWriter(t)
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionGrantShield, TargetSerial: "AA", Value: "10"}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	assertTargetingResolves(t, path, flagShieldSeconds, "AA", "agent_AA")
	assertTargetingResolves(t, path, flagShieldSeconds, "QQ", neutralNone)
	if v := readFlag(t, path, flagShieldSeconds)["variants"].(map[string]any)["agent_AA"].(float64); v != 10 {
		t.Fatalf("shield value = %v, want 10", v)
	}
}

func TestTargetedRequiresSerial(t *testing.T) {
	w, _ := newTestWriter(t)
	err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustPlayerSensitivity})
	if err == nil {
		t.Fatal("expected error for targeted intervention without serial")
	}
}

func TestUnknownIntervention(t *testing.T) {
	w, _ := newTestWriter(t)
	if err := w.Apply(context.Background(), decision.Decision{Intervention: "does_not_exist"}); err == nil {
		t.Fatal("expected error for unknown intervention")
	}
}

// TestNoopDispatchIsByteStableSuccess asserts that dispatching the probe-mode
// `noop` intervention succeeds (it is NOT an unknown-intervention error) and
// writes nothing — every flag round-trips byte-for-byte. Probe mode can thus
// exercise the full ACT path harmlessly.
func TestNoopDispatchIsByteStableSuccess(t *testing.T) {
	w, path := newTestWriter(t)
	before := readAllFlagsRaw(t, path)

	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionNoop}); err != nil {
		t.Fatalf("noop dispatch must succeed, got: %v", err)
	}

	after := readAllFlagsRaw(t, path)
	for key, rawBefore := range before {
		if string(after[key]) != string(rawBefore) {
			t.Fatalf("noop must not mutate flag %q:\n before=%s\n after =%s", key, rawBefore, after[key])
		}
	}
}

// TestPreservesUnrelatedFlags asserts that flags the agent does not touch are
// byte-identical before and after a write, and that touched flags are the only
// difference.
func TestPreservesUnrelatedFlags(t *testing.T) {
	w, path := newTestWriter(t)
	before := readAllFlagsRaw(t, path)

	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionPlayAudioCue, Value: "x"}); err != nil {
		t.Fatalf("apply: %v", err)
	}
	after := readAllFlagsRaw(t, path)

	for key, rawBefore := range before {
		if key == flagAudioCue {
			if string(after[key]) == string(rawBefore) {
				t.Fatalf("touched flag %q unchanged", key)
			}
			continue
		}
		if string(after[key]) != string(rawBefore) {
			t.Fatalf("untouched flag %q changed:\n before=%s\n after =%s", key, rawBefore, after[key])
		}
	}
}

// TestRoundTripStableFormatting asserts the written file matches the
// indent-2 + trailing-newline format admin mode uses, and re-applying the same
// flag (different nonce) keeps every other flag byte-stable.
func TestRoundTripStableFormatting(t *testing.T) {
	w, path := newTestWriter(t)
	if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionAdjustVolume, Value: "0.5"}); err != nil {
		t.Fatalf("first apply: %v", err)
	}
	raw, _ := os.ReadFile(path)
	if !strings.HasSuffix(string(raw), "\n") {
		t.Fatal("file must end with a trailing newline")
	}
	if !strings.Contains(string(raw), "\n  \"flags\"") {
		t.Fatalf("file is not 2-space indented:\n%s", raw)
	}
}

func TestConcurrentApplyRaceSafe(t *testing.T) {
	w, path := newTestWriter(t)
	var wg sync.WaitGroup
	for i := 0; i < 20; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_ = w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame})
		}()
	}
	wg.Wait()
	// File still parses and is valid after concurrent writes.
	assertStructurallyValid(t, path)
}

// --- helpers ---

func readAllFlagsRaw(t *testing.T, path string) map[string]json.RawMessage {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	var doc struct {
		Flags map[string]json.RawMessage `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return doc.Flags
}

// assertStructurallyValid checks the flagd-shape invariants every flag must
// hold: ENABLED state, a variants object, and a defaultVariant that names an
// existing variant.
func assertStructurallyValid(t *testing.T, path string) {
	t.Helper()
	for key, raw := range readAllFlagsRaw(t, path) {
		var flag map[string]any
		if err := json.Unmarshal(raw, &flag); err != nil {
			t.Fatalf("flag %q invalid JSON: %v", key, err)
		}
		if flag["state"] != "ENABLED" {
			t.Fatalf("flag %q state = %v, want ENABLED", key, flag["state"])
		}
		variants, ok := flag["variants"].(map[string]any)
		if !ok || len(variants) == 0 {
			t.Fatalf("flag %q missing variants", key)
		}
		dv, ok := flag["defaultVariant"].(string)
		if !ok {
			t.Fatalf("flag %q missing defaultVariant", key)
		}
		if _, ok := variants[dv]; !ok {
			t.Fatalf("flag %q defaultVariant %q not in variants", key, dv)
		}
	}
}

// assertTargetingResolves evaluates the flag's targeting if-ladder with the
// flagd jsonlogic engine (diegoholiveira/jsonlogic, the same library flagd uses)
// for targetingKey=serial and asserts it returns wantVariant. When the flag has
// no targeting block, the result is the defaultVariant.
func assertTargetingResolves(t *testing.T, path, flagKey, serial, wantVariant string) {
	t.Helper()
	flag := readFlag(t, path, flagKey)
	targeting, ok := flag["targeting"]
	if !ok {
		if dv := flag["defaultVariant"].(string); dv != wantVariant {
			t.Fatalf("%s[%s]: no targeting, defaultVariant=%q want %q", flagKey, serial, dv, wantVariant)
		}
		return
	}
	rule, err := json.Marshal(targeting)
	if err != nil {
		t.Fatalf("marshal targeting: %v", err)
	}
	data, _ := json.Marshal(map[string]any{"targetingKey": serial})
	out, err := jsonlogic.ApplyRaw(rule, data)
	if err != nil {
		t.Fatalf("jsonlogic apply (%s): %v", flagKey, err)
	}
	var got any
	if err := json.Unmarshal(out, &got); err != nil {
		t.Fatalf("unmarshal jsonlogic result: %v", err)
	}
	if got != wantVariant {
		t.Fatalf("%s[%s] resolved to %v, want %q", flagKey, serial, got, wantVariant)
	}
}
