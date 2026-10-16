// Package patterngame holds the agent side of repeatable pattern-driven shadow
// games (UC-046, #1258): the archetype parameters and their validation, the
// experiment.mock_archetypes selection, the safety cap, the per-game chaos seed of
// the rules engine, the flag digest, and a process-wide Tracker of live pattern
// games that the decision loop (remote-backend marking, decision recording) and
// the shadow runner (replay record) share.
//
// The package imports nothing from the agent so decision/, experiment/ and
// gamerunner/ can depend on it without a cycle.
package patterngame

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash/fnv"
	"os"
	"strings"
)

// Flag keys (flags.md §3.1).
const (
	// SelectionFlagKey is the comma-separated list of archetype names (string flag).
	SelectionFlagKey = "experiment.mock_archetypes"
	// ArchetypeFlagKey is the object flag whose variants are the archetypes; the
	// variant is chosen by the evaluation-context attribute ArchetypeContextKey.
	ArchetypeFlagKey = "experiment.mock_archetype"
	// ArchetypeContextKey is the evaluation-context attribute naming the variant.
	ArchetypeContextKey = "archetype"
	// CapFlagKey is the safety cap in seconds (integer flag).
	CapFlagKey = "experiment.max_game_seconds"
)

// SupportedMode is the only game mode pattern games run in v1 (C-021).
const SupportedMode = "JoustFFA"

// Safety cap bounds (UC-046 BR-013).
const (
	DefaultMaxGameSeconds = 300
	MinMaxGameSeconds     = 30
	MaxMaxGameSeconds     = 3600
)

// Non-exact reasons (UC-046 BR-008).
const (
	ReasonRemoteBackend  = "remote_backend"
	ReasonFrameOverrun   = "frame_overrun"
	ReasonConcurrentGame = "concurrent_game"
	ReasonKillSwitch     = "kill_switch"
)

// Game outcomes on the replay record (telemetry.md §9 pattern.outcome).
const (
	OutcomeWinner  = "winner"
	OutcomeTimeout = "timeout"
	OutcomeAborted = "aborted"
	OutcomeError   = "error"
)

// Experiment discard reasons (UC-046 A2, A4, A13).
const (
	DiscardArchetypeInvalid = "pattern_archetype_invalid"
	DiscardSelectionSmall   = "pattern_selection_too_small"
	DiscardModeUnsupported  = "pattern_mode_unsupported"
)

const (
	selectionNone   = "none"
	chaosSeedDomain = "agent.rules"
)

// ParseSelection splits the selection flag value into archetype names. "", "none"
// (any case) and blank entries yield no names (today's noise, UC-046 A1). Order is
// preserved and duplicates are kept so the caller can reject them (A4).
func ParseSelection(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" || strings.EqualFold(raw, selectionNone) {
		return nil
	}
	var names []string
	for _, part := range strings.Split(raw, ",") {
		if name := strings.TrimSpace(part); name != "" {
			names = append(names, name)
		}
	}
	return names
}

// HasDuplicate reports the first name that appears twice, or "".
func HasDuplicate(names []string) string {
	seen := make(map[string]struct{}, len(names))
	for _, n := range names {
		if _, ok := seen[n]; ok {
			return n
		}
		seen[n] = struct{}{}
	}
	return ""
}

// ClampCap returns the safety cap in seconds: values outside 30..3600 fall back to
// the default 300 (flags.md §3.1).
func ClampCap(seconds int) int {
	if seconds < MinMaxGameSeconds || seconds > MaxMaxGameSeconds {
		return DefaultMaxGameSeconds
	}
	return seconds
}

// ChaosSeed derives the rules engine's per-game chaos seed (UC-046 BR-007):
// FNV-1a/64 over the game's rng_seed (8 bytes little-endian), a 0 separator and
// "agent.rules".
func ChaosSeed(rngSeed uint64) uint64 {
	h := fnv.New64a()
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], rngSeed)
	_, _ = h.Write(b[:])
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(chaosSeedDomain))
	return h.Sum64()
}

// ArchetypeSeed is the one-shot runner's default seed when SHADOW_GAME_SEED is
// unset (UC-046 BR-006): FNV-1a/64 of the archetype name, never 0.
func ArchetypeSeed(name string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(name))
	if s := h.Sum64(); s != 0 {
		return s
	}
	return 1
}

// FlagDigest is UC-046 BR-011: SHA-256 (lowercase hex) of the bytes of each file
// in order. A missing file contributes its path and "<missing>" so the digest
// still differs from a run where the file existed.
func FlagDigest(paths ...string) string {
	h := sha256.New()
	for _, p := range paths {
		data, err := os.ReadFile(p) // #nosec G304 -- operator-configured flag file paths
		if err != nil {
			_, _ = fmt.Fprintf(h, "%s<missing>", p)
			continue
		}
		_, _ = h.Write(data)
	}
	return hex.EncodeToString(h.Sum(nil))
}
