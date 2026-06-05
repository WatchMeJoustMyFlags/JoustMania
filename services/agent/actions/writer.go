// Package actions is the agent's ActionSink: it APPLIES decisions by rewriting
// the flagd `interventions` flag file (services/flagd/interventions.json). There
// is no gRPC to the game services — the game coordinator watches the file and
// converges on its contents (docs/research/722-intervention-surface.md §8).
//
// Two flag shapes are written, matching the game-side reader contract:
//
//   - State-shaped interventions (music_tempo_override, global_sensitivity_override,
//     volume_override) and per-player state (player_sensitivity_factor,
//     shield_seconds): a single dedicated variant is overwritten with the typed
//     value and defaultVariant flips to it; reverting flips defaultVariant back to
//     the neutral variant (none/default). Per-player state additionally writes a
//     flagd targeting if-ladder keyed on targetingKey=serial.
//   - Edge-triggered one-shots (eliminate_player, revive_player, audio_cue,
//     controller_effect, end_game): a single variant holds "<nonce>:<payload>"
//     (end_game is nonce-only) and defaultVariant flips to it. A fresh unique
//     nonce per dispatch makes the reader apply exactly once on nonce change.
//
// The write is read-modify-write IN PLACE (no temp+rename — rename triggers
// EBUSY on docker bind mounts while flagd holds an inotify watch; same semantics
// as lib/flag_config_writer.py). The agent is the sole writer; a process mutex
// serializes concurrent dispatches.
package actions

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strconv"
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"

	"github.com/joustmania/agent/decision"
)

// Flag keys in the interventions domain (services/flagd/interventions.json).
const (
	flagMusicTempoOverride        = "music_tempo_override"
	flagGlobalSensitivityOverride = "global_sensitivity_override"
	flagPlayerSensitivityFactor   = "player_sensitivity_factor"
	flagShieldSeconds             = "shield_seconds"
	flagVolumeOverride            = "volume_override"
	flagGlobalDifficultyFactor    = "global_difficulty_factor" // #766 F6
	flagPacingProfile             = "pacing_profile"           // #766 F6
	flagEliminatePlayer           = "eliminate_player"
	flagRevivePlayer              = "revive_player"
	flagAudioCue                  = "audio_cue"
	flagControllerEffect          = "controller_effect"
	flagEndGame                   = "end_game"
)

// activeVariant is the single dedicated variant name the agent overwrites for
// every flag it drives (state values and edge "<nonce>:<payload>" strings).
const activeVariant = "active"

// Neutral variant names per flag (reverting a state flag flips defaultVariant
// back to these).
const (
	neutralNone    = "none"
	neutralDefault = "default"
)

// DefaultPath is the in-container interventions file path. Override with
// INTERVENTIONS_FLAG_PATH.
const DefaultPath = "/etc/flagd/interventions.json"

// meterName scopes the Writer's OTel instruments.
const meterName = "github.com/joustmania/agent/actions"

// Result label values for agent_intervention_writes_total. Cardinality is
// bounded to these two values (no error strings) to keep label cardinality low
// per .claude/rules/development.md.
const (
	resultOK    = "ok"
	resultError = "error"
)

// Per-intervention defaults used when a Decision carries no explicit Value. The
// rules engine (#726) leaves Value empty today, so these are the contract.
const (
	defaultAudioCue          = "agent_cue"
	defaultControllerEffect  = "rumble"
	defaultMusicTempo        = 1.15 // matches interventions.json "medium"
	defaultVolume            = 0.7  // matches "normal"
	defaultGlobalSensitivity = 2    // matches "medium"
	defaultPlayerSensitivity = 1.5  // matches "handicap"
	defaultShieldSeconds     = 5    // matches "default"
	defaultGlobalDifficulty  = 1.5  // matches interventions.json "hard" (#766 F6)
	defaultPacingProfile     = "frantic"
)

// Writer is the production ActionSink. It is safe for concurrent use; the agent
// is the only writer of the interventions file.
type Writer struct {
	path  string
	log   *slog.Logger
	nonce *nonceGen

	// writes counts flag-write outcomes (agent_intervention_writes_total),
	// labeled result=ok|error. Sourced from the global meter provider so it is a
	// no-op recorder until initMetrics wires the OTLP exporter.
	writes otelmetric.Int64Counter

	mu sync.Mutex
}

// NewWriter builds a Writer for the interventions file at path. path "" uses
// DefaultPath. log "" uses slog.Default().
func NewWriter(path string, log *slog.Logger) *Writer {
	if path == "" {
		path = DefaultPath
	}
	if log == nil {
		log = slog.Default()
	}
	return &Writer{
		path:   path,
		log:    log,
		nonce:  newNonceGen(),
		writes: newWritesCounter(otel.GetMeterProvider()),
	}
}

// newWritesCounter builds the agent_intervention_writes_total counter from the
// given meter provider. An instrument-creation error yields a no-op counter so
// the Writer always has a usable instrument.
func newWritesCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(meterName).Int64Counter(
		"agent_intervention_writes_total",
		otelmetric.WithDescription("Total intervention flag-file writes by the agent ActionSink, labeled by outcome"),
	)
	if err != nil {
		// Int64Counter only errors on bad config; fall back to a no-op so callers
		// never have to nil-check.
		c, _ = metricnoop.NewMeterProvider().Meter(meterName).Int64Counter("agent_intervention_writes_total")
	}
	return c
}

// NewWriterFromEnv builds a Writer using INTERVENTIONS_FLAG_PATH (default
// DefaultPath).
func NewWriterFromEnv(log *slog.Logger) *Writer {
	return NewWriter(os.Getenv("INTERVENTIONS_FLAG_PATH"), log)
}

// Apply implements decision.ActionSink: it maps the decision's intervention type
// to a flag mutation and rewrites the interventions file in place.
func (w *Writer) Apply(ctx context.Context, d decision.Decision) error {
	if err := w.edit(func(doc *orderedDoc) error { return w.mutate(doc, d) }); err != nil {
		w.recordWrite(ctx, resultError)
		w.log.Error("agent.action_write_failed",
			"intervention", d.Intervention,
			"target_serial", d.TargetSerial,
			"path", w.path,
			"error", err)
		return err
	}
	w.recordWrite(ctx, resultOK)
	w.log.Debug("agent.action_written",
		"intervention", d.Intervention,
		"target_serial", d.TargetSerial,
		"path", w.path)
	return nil
}

// recordWrite increments agent_intervention_writes_total for one Apply outcome.
func (w *Writer) recordWrite(ctx context.Context, result string) {
	w.writes.Add(ctx, 1, otelmetric.WithAttributes(attribute.String("result", result)))
}

// edit runs one read-modify-write cycle under the process mutex: read the file,
// apply fn to the order-preserving document, and write it back in place.
func (w *Writer) edit(fn func(*orderedDoc) error) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	raw, err := os.ReadFile(w.path)
	if err != nil {
		return fmt.Errorf("read interventions file %q: %w", w.path, err)
	}
	doc, err := parseDoc(raw)
	if err != nil {
		return err
	}
	if err := fn(doc); err != nil {
		return err
	}
	out, err := doc.marshal()
	if err != nil {
		return err
	}
	return w.writeInPlace(out)
}

// RevertState flips a session-scoped state flag back to its neutral variant
// (music_tempo_override/global_sensitivity_override/volume_override). It is the
// reversion half of the state-shaped contract (§8): the game converges back on
// "no override". Safe for concurrent use.
func (w *Writer) RevertState(flagKey string) error {
	neutral, ok := stateNeutral[flagKey]
	if !ok {
		return fmt.Errorf("RevertState: %q is not a session state flag", flagKey)
	}
	return w.edit(func(doc *orderedDoc) error {
		f, err := doc.flag(flagKey)
		if err != nil {
			return err
		}
		if err := f.set("defaultVariant", neutral); err != nil {
			return err
		}
		return doc.putFlag(flagKey, f)
	})
}

// RevertTargeted removes one player from a per-player state flag's targeting
// if-ladder (player_sensitivity_factor/shield_seconds). The player's variant
// and ladder branch are dropped; once no agent-driven serials remain the
// targeting block is removed entirely. Unmatched serials fall through to the
// neutral defaultVariant. Safe for concurrent use.
func (w *Writer) RevertTargeted(flagKey, serial string) error {
	neutral, ok := targetedNeutral[flagKey]
	if !ok {
		return fmt.Errorf("RevertTargeted: %q is not a per-player state flag", flagKey)
	}
	return w.edit(func(doc *orderedDoc) error {
		f, err := doc.flag(flagKey)
		if err != nil {
			return err
		}
		variants := map[string]float64{}
		if _, err := f.get("variants", &variants); err != nil {
			return err
		}
		delete(variants, serialVariant(serial))
		if err := f.set("variants", variants); err != nil {
			return err
		}
		if ladder := buildTargeting(variants, neutral); ladder == nil {
			f.delete("targeting")
		} else if err := f.set("targeting", ladder); err != nil {
			return err
		}
		if err := f.set("defaultVariant", neutral); err != nil {
			return err
		}
		return doc.putFlag(flagKey, f)
	})
}

// stateNeutral / targetedNeutral name the neutral variant for each revertible
// flag.
var stateNeutral = map[string]string{
	flagMusicTempoOverride:        neutralNone,
	flagGlobalSensitivityOverride: neutralNone,
	flagVolumeOverride:            neutralNone,
	flagGlobalDifficultyFactor:    neutralDefault, // #766 F6 (neutral = 1.0 "default")
	flagPacingProfile:             neutralNone,    // #766 F6 (neutral = "none")
}

var targetedNeutral = map[string]string{
	flagPlayerSensitivityFactor: neutralDefault,
	flagShieldSeconds:           neutralNone,
}

// mutate applies one decision to the in-memory document.
func (w *Writer) mutate(doc *orderedDoc, d decision.Decision) error {
	switch d.Intervention {
	// Edge-triggered one-shots.
	case decision.InterventionPlayAudioCue:
		return w.setEdge(doc, flagAudioCue, w.payloadOr(d.Value, defaultAudioCue))
	case decision.InterventionSendControllerEffect:
		effect := w.payloadOr(d.Value, defaultControllerEffect)
		return w.setEdge(doc, flagControllerEffect, d.TargetSerial+":"+effect)
	case decision.InterventionEliminatePlayer:
		return w.setEdge(doc, flagEliminatePlayer, d.TargetSerial)
	case decision.InterventionRevivePlayer:
		return w.setEdge(doc, flagRevivePlayer, d.TargetSerial)
	case decision.InterventionEndGame:
		return w.setEdge(doc, flagEndGame, "") // nonce-only

	// Session-scoped state-shaped overrides.
	case decision.InterventionAdjustMusicTempo:
		return w.setState(doc, flagMusicTempoOverride, w.numOr(d.Value, defaultMusicTempo))
	case decision.InterventionAdjustVolume:
		return w.setState(doc, flagVolumeOverride, w.numOr(d.Value, defaultVolume))
	case decision.InterventionAdjustGlobalSensitivity:
		return w.setState(doc, flagGlobalSensitivityOverride, w.numOr(d.Value, defaultGlobalSensitivity))
	case decision.InterventionAdjustGlobalDifficulty: // #766 F6
		return w.setState(doc, flagGlobalDifficultyFactor, w.numOr(d.Value, defaultGlobalDifficulty))
	case decision.InterventionSetPacingProfile: // #766 F6 (string state-shaped)
		return w.setStateString(doc, flagPacingProfile, w.payloadOr(d.Value, defaultPacingProfile))

	// Per-player state-shaped overrides (flagd targeting if-ladder).
	case decision.InterventionAdjustPlayerSensitivity:
		return w.setTargeted(doc, flagPlayerSensitivityFactor, neutralDefault, d.TargetSerial, w.numOr(d.Value, defaultPlayerSensitivity))
	case decision.InterventionGrantShield:
		return w.setTargeted(doc, flagShieldSeconds, neutralNone, d.TargetSerial, w.numOr(d.Value, defaultShieldSeconds))

	default:
		return fmt.Errorf("unknown intervention %q", d.Intervention)
	}
}

// setEdge writes an edge-triggered flag: overwrite the "active" variant with
// "<nonce>:<payload>" ("<nonce>" alone when payload is empty) and flip
// defaultVariant to it. A fresh nonce per call guarantees the reader sees a
// change and applies exactly once.
func (w *Writer) setEdge(doc *orderedDoc, flagKey, payload string) error {
	f, err := doc.flag(flagKey)
	if err != nil {
		return err
	}
	value := w.nonce.next()
	if payload != "" {
		value += ":" + payload
	}
	if err := setVariant(f, activeVariant, value); err != nil {
		return err
	}
	if err := f.set("defaultVariant", activeVariant); err != nil {
		return err
	}
	return doc.putFlag(flagKey, f)
}

// setState writes a session-scoped state-shaped flag: overwrite the "active"
// variant with the typed value and flip defaultVariant to it.
func (w *Writer) setState(doc *orderedDoc, flagKey string, value float64) error {
	f, err := doc.flag(flagKey)
	if err != nil {
		return err
	}
	if err := setVariant(f, activeVariant, value); err != nil {
		return err
	}
	if err := f.set("defaultVariant", activeVariant); err != nil {
		return err
	}
	return doc.putFlag(flagKey, f)
}

// setStateString writes a session-scoped STRING state-shaped flag (#766 F6
// pacing_profile): overwrite the "active" variant with the string value and flip
// defaultVariant to it. Mirrors setState but for string-typed flags.
func (w *Writer) setStateString(doc *orderedDoc, flagKey, value string) error {
	f, err := doc.flag(flagKey)
	if err != nil {
		return err
	}
	if err := setVariant(f, activeVariant, value); err != nil {
		return err
	}
	if err := f.set("defaultVariant", activeVariant); err != nil {
		return err
	}
	return doc.putFlag(flagKey, f)
}

// setTargeted writes a per-player state-shaped flag via a flagd targeting
// if-ladder keyed on targetingKey=serial. Each driven serial gets its own
// value-bearing variant ("agent_<serial>"); the if-ladder maps serial -> that
// variant, and unmatched serials fall through to defaultVariant (the neutral
// variant). Existing per-serial variants and ladder branches are preserved, so
// driving a second player does not disturb the first.
func (w *Writer) setTargeted(doc *orderedDoc, flagKey, neutral, serial string, value float64) error {
	if serial == "" {
		return fmt.Errorf("targeted intervention on %q requires a target serial", flagKey)
	}
	f, err := doc.flag(flagKey)
	if err != nil {
		return err
	}

	// Read existing per-serial variant->value mapping so we merge, not clobber.
	variants := map[string]float64{}
	if _, err := f.get("variants", &variants); err != nil {
		return err
	}
	variants[serialVariant(serial)] = value
	if err := f.set("variants", variants); err != nil {
		return err
	}

	// Rebuild the deterministic if-ladder from every agent_<serial> variant.
	ladder := buildTargeting(variants, neutral)
	if ladder == nil {
		// No agent-driven serials remain: drop targeting entirely.
		f.delete("targeting")
	} else if err := f.set("targeting", ladder); err != nil {
		return err
	}
	if err := f.set("defaultVariant", neutral); err != nil {
		return err
	}
	return doc.putFlag(flagKey, f)
}

// payloadOr returns v, or the default when v is empty.
func (w *Writer) payloadOr(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

// numOr parses v as a float, or returns def when v is empty/unparseable.
func (w *Writer) numOr(v string, def float64) float64 {
	if v == "" {
		return def
	}
	n, err := strconv.ParseFloat(v, 64)
	if err != nil {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	return n
}

// writeInPlace overwrites the file at the same path WITHOUT temp+rename. Rename
// over a docker bind mount that flagd is watching fails with EBUSY; an in-place
// truncate+write is the proven admin-mode pattern (lib/flag_config_writer.py).
func (w *Writer) writeInPlace(data []byte) error {
	f, err := os.OpenFile(w.path, os.O_WRONLY|os.O_TRUNC|os.O_CREATE, 0o644)
	if err != nil {
		return fmt.Errorf("open interventions file for write: %w", err)
	}
	if _, err := f.Write(data); err != nil {
		_ = f.Close()
		return fmt.Errorf("write interventions file: %w", err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("close interventions file: %w", err)
	}
	return nil
}

// setVariant overwrites the named variant with value (number or string).
func setVariant(f *flagObj, name string, value any) error {
	variants := map[string]any{}
	if _, err := f.get("variants", &variants); err != nil {
		return err
	}
	variants[name] = value
	return f.set("variants", variants)
}

// serialVariant is the variant name for a per-player targeted value.
func serialVariant(serial string) string { return "agent_" + serial }

// buildTargeting renders a flagd targeting block: a JsonLogic if-ladder mapping
// targetingKey==serial -> the serial's variant. Branches are emitted in sorted
// serial order for deterministic output. Returns nil when no agent_<serial>
// variants exist (targeting should be removed). The trailing else is neutral,
// so unmatched serials evaluate to defaultVariant anyway — explicit neutral
// keeps the ladder self-describing.
func buildTargeting(variants map[string]float64, neutral string) map[string]any {
	serials := make([]string, 0)
	for name := range variants {
		if s, ok := agentSerial(name); ok {
			serials = append(serials, s)
		}
	}
	if len(serials) == 0 {
		return nil
	}
	sort.Strings(serials)

	// flagd's "if" is [cond1, then1, cond2, then2, ..., else]. Build it nested:
	// if(eq(key,s0), v0, if(eq(key,s1), v1, ... neutral)).
	expr := any(neutral)
	for i := len(serials) - 1; i >= 0; i-- {
		s := serials[i]
		expr = map[string]any{
			"if": []any{
				map[string]any{"===": []any{map[string]any{"var": "targetingKey"}, s}},
				serialVariant(s),
				expr,
			},
		}
	}
	m, ok := expr.(map[string]any)
	if !ok {
		// Single neutral string (no serials) — unreachable given the guard above.
		return nil
	}
	return m
}

// agentSerial extracts the serial from an "agent_<serial>" variant name.
func agentSerial(variant string) (string, bool) {
	const p = "agent_"
	if len(variant) > len(p) && variant[:len(p)] == p {
		return variant[len(p):], true
	}
	return "", false
}
