package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/patterngame"
)

// pattern_wiring.go connects the UC-046 pattern-game pieces (#1258) to the agent:
// the concurrency probe, the experiment pinner, the flag digest and the one-shot
// runner's archetype selection and replay-record log line.

// Env of the one-shot runner (flags.md §3.1).
const (
	envShadowArchetype = "SHADOW_GAME_ARCHETYPE"
	envShadowSeed      = "SHADOW_GAME_SEED"
)

// otherGameLive reports whether any game other than gameID is active in the
// telemetry multiplexer (UC-046 A8).
func otherGameLive(mux *gamecontext.Multiplexer) func(gameID string) bool {
	return func(gameID string) bool {
		for _, id := range mux.Partitions() {
			if id == gameID {
				continue
			}
			snap, ok := mux.Snapshot(id)
			if ok && snap.Session.GameActive != nil && *snap.Session.GameActive {
				return true
			}
		}
		return false
	}
}

// flagDigestPaths are the files of UC-046 BR-011: agent.json then game.json.
func flagDigestPaths() []string {
	dir := getEnv("FLAGD_FLAG_DIR", "/etc/flagd")
	return []string{
		filepath.Join(dir, "agent.json"),
		getEnv("GAME_FLAG_PATH", experiment.DefaultGamePath),
	}
}

// flagPatternPinner is the production experiment.PatternPinner: it reads the
// live agent flags at an experiment's first spawn (UC-046 step 2).
type flagPatternPinner struct {
	flags    *flags.Flags
	mode     string
	minPairs func(ctx context.Context) int
}

// Pin implements experiment.PatternPinner.
func (p flagPatternPinner) Pin(ctx context.Context) patterngame.Pin {
	return patterngame.Resolve(ctx, p.flags, p.mode, p.minPairs(ctx), "")
}

// oneShotPatternSpec turns a one-shot shadow spec into a pattern game when
// SHADOW_GAME_ARCHETYPE or experiment.mock_archetypes selects an archetype (the
// first name of a list). ok is false for noise; err carries a discard reason.
func oneShotPatternSpec(ctx context.Context, f *flags.Flags, spec gamerunner.Spec) (gamerunner.Spec, bool, string) {
	if f == nil {
		return spec, false, ""
	}
	override := strings.TrimSpace(os.Getenv(envShadowArchetype))
	if override == "" {
		if names := patterngame.ParseSelection(f.MockArchetypes(ctx)); len(names) > 0 {
			override = names[0]
		}
	}
	if override == "" {
		return spec, false, ""
	}
	pin := patterngame.Resolve(ctx, f, spec.GameName, 0, override)
	if !pin.Active() {
		if pin.DiscardReason == "" {
			return spec, false, ""
		}
		return spec, false, pin.DiscardReason + ": " + strings.Join(pin.Errors, "; ")
	}
	a := pin.Archetypes[0]
	spec.Pattern = a
	spec.MaxGameSeconds = pin.MaxGameSeconds
	spec.PairIndex = -1
	spec.FlagDigest = patterngame.FlagDigest(flagDigestPaths()...)
	spec.Seed = patterngame.ArchetypeSeed(a.Name)
	if v := strings.TrimSpace(os.Getenv(envShadowSeed)); v != "" {
		if seed, err := strconv.ParseUint(v, 10, 64); err == nil && seed != 0 {
			spec.Seed = seed
		}
	}
	return spec, true, ""
}

// replayRecordJSON is the pattern_replay_record log payload read by
// scripts/pattern-replay-check.sh (telemetry.md §9 "Replay check").
type replayRecordJSON struct {
	GameID           string     `json:"game_id"`
	Archetype        string     `json:"archetype"`
	ParamsDigest     string     `json:"params_digest"`
	RngSeed          string     `json:"rng_seed"`
	FlagDigest       string     `json:"flag_digest"`
	Outcome          string     `json:"outcome"`
	EliminationOrder []int      `json:"elimination_order"`
	WinnerSlot       int        `json:"winner_slot"`
	SignalHashes     []string   `json:"signal_hashes"`
	Checkpoints      [][]string `json:"signal_checkpoints"`
	ReplayExact      bool       `json:"replay_exact"`
	NonExactReasons  []string   `json:"non_exact_reasons"`
	Decisions        []string   `json:"decisions"`
}

// logReplayRecord prints the one-shot game's replay record as one JSON log value.
func logReplayRecord(logger *slog.Logger, rec patterngame.Record) {
	payload, err := json.Marshal(replayRecordJSON{
		GameID:           rec.GameID,
		Archetype:        rec.Archetype.Name,
		ParamsDigest:     rec.Archetype.Digest,
		RngSeed:          strconv.FormatUint(rec.Seed, 10),
		FlagDigest:       rec.FlagDigest,
		Outcome:          rec.Outcome,
		EliminationOrder: nonNilInts(rec.EliminationOrder),
		WinnerSlot:       rec.WinnerSlot,
		SignalHashes:     rec.SignalHashes(),
		Checkpoints:      rec.Checkpoints(),
		ReplayExact:      rec.Exact(),
		NonExactReasons:  nonNilStrings(rec.NonExactReasons),
		Decisions:        nonNilStrings(rec.DecisionSequence()),
	})
	if err != nil {
		logger.Warn("pattern replay record could not be encoded", "error", err)
		return
	}
	logger.Info("pattern_replay_record", "record", string(payload))
	// One plain line as well, so scripts/pattern-replay-check.sh needs no log parsing.
	_, _ = fmt.Fprintln(os.Stdout, replayRecordPrefix+string(payload))
}

// replayRecordPrefix starts the plain stdout replay-record line.
const replayRecordPrefix = "PATTERN_REPLAY_RECORD "

func nonNilInts(v []int) []int {
	if v == nil {
		return []int{}
	}
	return v
}

func nonNilStrings(v []string) []string {
	if v == nil {
		return []string{}
	}
	return v
}
