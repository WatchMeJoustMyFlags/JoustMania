package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/patterngame"
	"github.com/joustmania/agent/promote"

	otelmetric "go.opentelemetry.io/otel/metric"
)

// pattern_wiring.go connects the UC-046 pattern-game pieces (#1258) to the agent:
// the concurrency probe, the experiment pinner, the flag digest and the one-shot
// runner's archetype selection and replay-record log line.

// Env of the one-shot runner (flags.md §3.1).
const (
	envShadowArchetype = "SHADOW_GAME_ARCHETYPE"
	envShadowSeed      = "SHADOW_GAME_SEED"
)

// liveStaleness is how fresh a telemetry partition must be to count as a live
// game (UC-046 BR-008; a partition without updates is a stale leftover, cf. #1020).
const liveStaleness = 10 * time.Second

// otherGameLive reports whether any game other than gameID is active in the
// telemetry multiplexer with an update in the last liveStaleness (UC-046 A8).
func otherGameLive(mux *gamecontext.Multiplexer) func(gameID string) bool {
	return otherGameLiveAt(mux, time.Now)
}

func otherGameLiveAt(mux *gamecontext.Multiplexer, now func() time.Time) func(gameID string) bool {
	return func(gameID string) bool {
		for _, id := range mux.Partitions() {
			if id == gameID {
				continue
			}
			snap, ok := mux.Snapshot(id)
			if !ok || snap.Session.GameActive == nil || !*snap.Session.GameActive {
				continue
			}
			if now().Sub(snap.UpdatedAt) > liveStaleness {
				continue // stale partition: no fresh data
			}
			return true
		}
		return false
	}
}

// ofrepURL is flagd's OFREP base URL (UC-046 BR-011): FLAGD_OFREP_URL, or
// http://<FLAGD_HOST>:8016.
func ofrepURL() string {
	if v := strings.TrimSpace(os.Getenv("FLAGD_OFREP_URL")); v != "" {
		return v
	}
	return "http://" + getEnv("FLAGD_HOST", "flagd") + ":8016"
}

var digestHTTPClient = &http.Client{Timeout: 5 * time.Second}

// resolvedFlagDigest computes UC-046 BR-011's digest for a shadow game with the
// given experiment id and arm ("" for a one-shot game).
func resolvedFlagDigest(ctx context.Context, experimentID, arm string) string {
	evalCtx := map[string]any{experiment.GameKindVar: "shadow"}
	if experimentID != "" {
		evalCtx[experiment.ExperimentIDVar] = experimentID
		evalCtx[experiment.ArmVar] = arm
	}
	return patterngame.ResolvedFlagDigest(ctx, digestHTTPClient, ofrepURL(), evalCtx)
}

// flagPatternPinner is the production experiment.PatternPinner: it reads the
// live agent flags in an experiment's first allocation pass (UC-046 step 2).
type flagPatternPinner struct {
	flags    *flags.Flags
	mode     string
	minPairs func(ctx context.Context) int
	log      *slog.Logger
	logOnce  sync.Once
}

// Pin implements experiment.PatternPinner.
func (p *flagPatternPinner) Pin(ctx context.Context) patterngame.Pin {
	pin := patterngame.Resolve(ctx, p.flags, p.mode, p.minPairs(ctx), "")
	p.logActive(pin)
	return pin
}

// logActive logs the archetype selection once at INFO (flags.md §3.1).
func (p *flagPatternPinner) logActive(pin patterngame.Pin) {
	if p.log == nil || !pin.Active() {
		return
	}
	p.logOnce.Do(func() {
		p.log.Info("Shadow experiments run on motion archetypes (UC-046)",
			"selection", pin.Selection, "archetypes", pin.Names(), "max_game_seconds", pin.MaxGameSeconds)
	})
}

// oneShotPatternSpec turns the one-shot shadow spec into a pattern game with
// SHADOW_GAME_ARCHETYPE or the first archetype of experiment.mock_archetypes. The
// one-shot runner is archetype-only too: ok false means the run is refused, with
// the reason in refused.
func oneShotPatternSpec(ctx context.Context, f *flags.Flags, spec gamerunner.Spec) (gamerunner.Spec, string) {
	if f == nil {
		return spec, patterngame.DiscardSelectionEmpty + ": no agent flags"
	}
	override := strings.TrimSpace(os.Getenv(envShadowArchetype))
	if override == "" {
		if names := patterngame.ParseSelection(f.MockArchetypes(ctx)); len(names) > 0 {
			override = names[0]
		}
	}
	pin := patterngame.Resolve(ctx, f, spec.GameName, 0, override)
	if !pin.Active() {
		return spec, pin.DiscardReason + ": " + strings.Join(pin.Errors, "; ")
	}
	a := pin.Archetypes[0]
	spec.Pattern = a
	spec.MaxGameSeconds = pin.MaxGameSeconds
	spec.PairIndex = -1
	spec.FlagDigest = resolvedFlagDigest(ctx, "", "")
	spec.Seed = patterngame.ArchetypeSeed(a.Name)
	if v := strings.TrimSpace(os.Getenv(envShadowSeed)); v != "" {
		if seed, err := strconv.ParseUint(v, 10, 64); err == nil && seed != 0 {
			spec.Seed = seed
		}
	}
	return spec, ""
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

// realGameLiveAt is the promoter's real-game probe (UC-043 A17 step 3): true when
// any telemetry partition reports an active game of kind `real` or without a kind
// (fail closed) that was updated in the last liveStaleness. A shadow game does not
// count. It is a prefilter: the flag service's 409 real_game_live is authoritative.
// mux is read at call time (main assigns it after the promoter is built); without
// one the probe errors, which the promoter treats as live.
func realGameLiveAt(mux func() *gamecontext.Multiplexer, now func() time.Time) promote.RealGameProbe {
	return func(context.Context) (bool, error) {
		m := mux()
		if m == nil {
			return true, errNoMultiplexer
		}
		for _, id := range m.Partitions() {
			snap, ok := m.Snapshot(id)
			if !ok || snap.Session.GameActive == nil || !*snap.Session.GameActive {
				continue
			}
			if now().Sub(snap.UpdatedAt) > liveStaleness || snap.GameKind == "shadow" {
				continue
			}
			return true, nil
		}
		return false, nil
	}
}

var errNoMultiplexer = errors.New("real-game probe: no telemetry multiplexer yet")

// gameLister is the flag-service read of the registered games.
type gameLister interface {
	ListGames(ctx context.Context) ([]flagservice.Game, error)
}

// realGameProbe is the promoter's probe (UC-043 A17 step 3): the telemetry
// prefilter first (live = live, no service call), then the flag service's
// authoritative registry, where any registered game whose kind is not exactly
// `shadow` is live, like its 409 real_game_live guard. An error of either counts
// as live (fail closed). The service check also covers the agent's own reverts,
// which the service never refuses (review of #2235): it closes the gap of stale
// or not yet exported telemetry, leaving only the time between the read and the
// DELETE.
func realGameProbe(telemetry promote.RealGameProbe, games gameLister) promote.RealGameProbe {
	return func(ctx context.Context) (bool, error) {
		if live, err := telemetry(ctx); live || err != nil {
			return true, err
		}
		if games == nil {
			return true, errNoGameRegistry
		}
		list, err := games.ListGames(ctx)
		if err != nil {
			return true, err
		}
		for _, g := range list {
			if g.GameKind != "shadow" {
				return true, nil
			}
		}
		return false, nil
	}
}

var errNoGameRegistry = errors.New("real-game probe: no flag-service game registry")

// wireDeferredPromotions wires the promoter's real-game probe and the deferred
// queue's gauges (UC-043 A17 step 3, opposer M4). main calls it before the
// experiment loop is built, which warns when the probe is missing.
func wireDeferredPromotions(p *promote.Promoter, mux func() *gamecontext.Multiplexer, games gameLister, mp otelmetric.MeterProvider, logger *slog.Logger) {
	if p == nil {
		return
	}
	if logger == nil {
		logger = slog.Default()
	}
	p.SetRealGameProbe(realGameProbe(realGameLiveAt(mux, time.Now), games))
	if err := p.RegisterDeferredMetrics(mp); err != nil {
		logger.Warn("code_improvement: deferred promotion gauges not registered", "error", err)
	}
}
