// Package actions is the agent's ActionSink: it APPLIES decisions as flag
// writes to the flag service (epic #1407, UC-053, docs/specs/services.md §7.2).
// The agent never writes a flag file (C-032, C-033); the flag service renders
// every write as targeting on the deciding game (and the player's serial), so a
// decision for one game never changes another (FR-136, FR-069).
//
// Three write shapes, matching the flag registry (docs/specs/flags.md §13.1):
//
//   - State interventions (music_tempo_override, global_sensitivity_override,
//     volume_override, global_difficulty_factor, pacing_profile, tempo_ramp,
//     auto_rubberband): PUT /interventions/<flag> with scope.game_id.
//   - Per-player levels and grants (player_sensitivity_factor,
//     player_handicap_factor; shield_seconds, toughness_boost_seconds,
//     soft_penalty_action): PUT with scope.game_id + scope.serial. The service
//     gives every write a fresh variant, so a re-grant is a change the
//     coordinator applies once (UC-040 BR-012).
//   - One-shot events (eliminate_player, revive_player, audio_cue,
//     controller_effect, end_game): POST appends an event to the game's event
//     list, the serial (or sound, effect) in the payload (FR-137, UC-053 BR-006).
//
// Every write has a 500 ms deadline and fails closed; a write rejected with 409
// game_not_live is retried once after 100 ms (UC-053 A6).
package actions

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/gamecontext"
)

// FlagSetInterventions is the flag set every intervention flag lives in.
const FlagSetInterventions = "interventions"

// Flag keys in the interventions set (services/flagd/interventions.json).
const (
	flagMusicTempoOverride        = "music_tempo_override"
	flagGlobalSensitivityOverride = "global_sensitivity_override"
	flagPlayerSensitivityFactor   = "player_sensitivity_factor"
	flagPlayerHandicapFactor      = "player_handicap_factor" // #1107 (#1103 MVP action 1)
	flagShieldSeconds             = "shield_seconds"
	flagToughnessBoostSeconds     = "toughness_boost_seconds" // #1129 (#1103 Phase 2)
	flagSoftPenaltyAction         = "soft_penalty_action"     // #1134 (#1103 Phase 3)
	flagVolumeOverride            = "volume_override"
	flagGlobalDifficultyFactor    = "global_difficulty_factor" // #766 F6
	flagPacingProfile             = "pacing_profile"           // #766 F6
	flagTempoRamp                 = "tempo_ramp"               // #1117 (#1103 MVP action 2)
	flagAutoRubberband            = "auto_rubberband"          // #1143 (#1103 Phase 3)
	flagEliminatePlayer           = "eliminate_player"
	flagRevivePlayer              = "revive_player"
	flagAudioCue                  = "audio_cue"
	flagControllerEffect          = "controller_effect"
	flagEndGame                   = "end_game"
)

// Neutral variant names per flag in interventions.json: the baseline value a
// game resolves when the agent wrote nothing for it.
const (
	neutralNone    = "none"
	neutralDefault = "default"
)

// writerLabel is the informational writer label on every agent write (D11).
const writerLabel = "agent"

// meterName scopes the Writer's OTel instruments.
const meterName = "github.com/joustmania/agent/actions"

// Result label values for agent_intervention_writes_total. Cardinality is
// bounded to these two values (no error strings).
const (
	resultOK    = "ok"
	resultError = "error"
)

// Per-intervention defaults used when a Decision carries no explicit Value. The
// rules engine (#726) leaves Value empty today, so these are the contract.
const (
	// defaultAudioCue is an audio-registry ALIAS of the lobby ready ping
	// (services/audio/sound_catalog.py, UC-040 BR-007): the audio service resolves
	// it, so it must stay listed there (services/audio/tests/test_sound_registry.py).
	defaultAudioCue          = "agent_cue"
	defaultControllerEffect  = "rumble"
	defaultMusicTempo        = 1.15 // matches interventions.json "medium"
	defaultVolume            = 0.7  // matches "normal"
	defaultGlobalSensitivity = 2    // matches "medium"
	defaultPlayerSensitivity = 1.5  // matches "handicap"
	defaultPlayerHandicap    = 1.25 // #1107: mild "help" (>1 harder to die), within [0.5,2.0]
	// defaultShieldSeconds is the fail-closed length for a grant the loop did not
	// stamp (UC-040 BR-029): the loop stamps every grant with the shield_seconds
	// "default" variant when the ShieldSecondsReader is wired (shield_seconds.go).
	defaultShieldSeconds    = decision.DefaultShieldSeconds
	defaultGlobalDifficulty = 1.5 // matches interventions.json "hard" (#766 F6)
	defaultPacingProfile    = "frantic"
	// defaultTempoRamp is the safe fallback ramp_tempo curve (#1117).
	defaultTempoRamp = "1.3:8:linear"
	// defaultToughnessBoost is the safe fallback toughness_boost directive (#1129).
	defaultToughnessBoost = "5:2.0"
	// defaultSoftPenalty is the safe fallback soft_penalty directive (#1134): the
	// gentlest, fully-recoverable "warn". Never escalates an unparseable value.
	defaultSoftPenalty = "warn"
	// defaultAutoRubberband is the safe fallback auto_rubberband strength (#1143).
	defaultAutoRubberband = "gentle"
)

// toughness_boost / soft_penalty / tempo_ramp bounds mirror the game side's
// clamps (lifecycle_handlers.py, games/base.py) and the flag registry, so the
// writer degrades an out-of-range model value to a safe default instead of
// sending a write the registry would reject.
const (
	toughnessBoostMaxSeconds = 30.0
	toughnessBoostFactorMin  = 1.0
	toughnessBoostFactorMax  = 2.0
	softPenaltyMaxSeconds    = 30.0
	softPenaltyFactorMin     = 0.5
	softPenaltyFactorMax     = 1.0
	tempoRampMaxSeconds      = 15.0 // games/base.py RAMP_TEMPO_MAX_SECONDS (#1840)
)

// validTempoRampCurves is the closed curve vocabulary (mirrors base.py).
var validTempoRampCurves = map[string]bool{"linear": true, "ease": true}

// FlagService is the part of the flag-service client the Writer uses.
type FlagService interface {
	Put(ctx context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error)
	Post(ctx context.Context, set, flag string, ev flagservice.Event) (flagservice.Result, error)
}

// Writer is the production ActionSink. It is safe for concurrent use.
type Writer struct {
	svc FlagService
	log *slog.Logger
	// writes counts write outcomes (agent_intervention_writes_total{result}).
	writes otelmetric.Int64Counter
}

// NewWriter builds a Writer over a flag-service client. log nil uses slog.Default().
func NewWriter(svc FlagService, log *slog.Logger) *Writer {
	if log == nil {
		log = slog.Default()
	}
	return &Writer{svc: svc, log: log, writes: newWritesCounter(otel.GetMeterProvider())}
}

// NewWriterFromEnv builds a Writer for the flag service at FLAG_SERVICE_URL.
func NewWriterFromEnv(log *slog.Logger) *Writer {
	return NewWriter(flagservice.NewFromEnv(), log)
}

func newWritesCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(meterName).Int64Counter(
		"agent_intervention_writes_total",
		otelmetric.WithDescription("Intervention writes the agent sent to the flag service, labeled by outcome and game_kind (primary|shadow, #2294)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(meterName).Int64Counter("agent_intervention_writes_total")
	}
	return c
}

// errNoGame refuses an intervention without its deciding game: every
// intervention is game-scoped (FR-136); an unscoped write would reach every game.
var errNoGame = errors.New("intervention requires the deciding game's id (FR-136)")

// coordinatorGameIDPrefix starts every game id the game coordinator issues,
// real or shadow. Any other id (the `session-<n>` partition for unlabeled
// signals) names no live game and is refused like an empty one (UC-040 A18).
const coordinatorGameIDPrefix = "game_"

// Apply implements decision.ActionSink: it maps the decision's intervention to
// one flag-service write for the deciding game.
func (w *Writer) Apply(ctx context.Context, d decision.Decision) error {
	if d.Intervention == decision.InterventionNoop {
		// Probe-mode synthetic intervention: a dispatched success with no write.
		return nil
	}
	err := w.write(ctx, d)
	if err != nil {
		w.recordWrite(ctx, d.GameKind, resultError)
		w.log.Error("agent.action_write_failed",
			"intervention", d.Intervention, "game_id", d.GameID, "target_serial", d.TargetSerial, "error", err)
		return err
	}
	w.recordWrite(ctx, d.GameKind, resultOK)
	w.log.Debug("agent.action_written", "intervention", d.Intervention, "game_id", d.GameID, "target_serial", d.TargetSerial)
	return nil
}

func (w *Writer) recordWrite(ctx context.Context, gameKind, result string) {
	w.writes.Add(ctx, 1, otelmetric.WithAttributes(
		attribute.String("game_kind", gamecontext.TelemetryGameKind(gameKind)),
		attribute.String("result", result)))
}

func (w *Writer) write(ctx context.Context, d decision.Decision) error {
	if d.GameID == "" {
		return errNoGame
	}
	if !strings.HasPrefix(d.GameID, coordinatorGameIDPrefix) {
		return fmt.Errorf("%w: %q is not a coordinator game id", errNoGame, d.GameID)
	}
	game := flagservice.Scope{GameID: d.GameID}
	player := flagservice.Scope{GameID: d.GameID, Serial: d.TargetSerial}
	switch d.Intervention {
	// One-shot events, appended to the game's event list.
	case decision.InterventionPlayAudioCue:
		return w.post(ctx, d, flagAudioCue, map[string]any{"sound": w.payloadOr(d.Value, defaultAudioCue)})
	case decision.InterventionSendControllerEffect:
		return w.post(ctx, d, flagControllerEffect, map[string]any{
			"serial": d.TargetSerial, "effect": w.payloadOr(d.Value, defaultControllerEffect),
		})
	case decision.InterventionEliminatePlayer:
		return w.post(ctx, d, flagEliminatePlayer, map[string]any{"serial": d.TargetSerial})
	case decision.InterventionRevivePlayer:
		return w.post(ctx, d, flagRevivePlayer, map[string]any{"serial": d.TargetSerial})
	case decision.InterventionEndGame:
		// The game is the scope (UC-040 BR-011): only the deciding game ends.
		return w.post(ctx, d, flagEndGame, map[string]any{})

	// Game-scoped state.
	case decision.InterventionAdjustMusicTempo:
		return w.put(ctx, d, flagMusicTempoOverride, game, w.numOr(d.Value, defaultMusicTempo))
	case decision.InterventionAdjustVolume:
		return w.put(ctx, d, flagVolumeOverride, game, w.numOr(d.Value, defaultVolume))
	case decision.InterventionAdjustGlobalSensitivity:
		return w.put(ctx, d, flagGlobalSensitivityOverride, game, int(w.numOr(d.Value, defaultGlobalSensitivity)))
	case decision.InterventionAdjustGlobalDifficulty: // #766 F6
		return w.put(ctx, d, flagGlobalDifficultyFactor, game, w.numOr(d.Value, defaultGlobalDifficulty))
	case decision.InterventionSetPacingProfile: // #766 F6
		return w.put(ctx, d, flagPacingProfile, game, w.payloadOr(d.Value, defaultPacingProfile))
	case decision.InterventionRampTempo: // #1117, shadow-only
		return w.put(ctx, d, flagTempoRamp, game, w.tempoRampOr(d.Value, defaultTempoRamp))
	case decision.InterventionAutoRubberband: // #1143, shadow-only
		return w.put(ctx, d, flagAutoRubberband, game, w.autoRubberbandOr(d.Value, defaultAutoRubberband))

	// Per-player levels (re-applied as a whole) and grants (fresh variant per write).
	case decision.InterventionAdjustPlayerSensitivity:
		return w.putPlayer(ctx, d, flagPlayerSensitivityFactor, player, w.numOr(d.Value, defaultPlayerSensitivity))
	case decision.InterventionSetPlayerHandicap: // #1107, shadow-only
		return w.putPlayer(ctx, d, flagPlayerHandicapFactor, player, w.numOr(d.Value, defaultPlayerHandicap))
	case decision.InterventionGrantShield:
		return w.putPlayer(ctx, d, flagShieldSeconds, player, w.numOr(d.Value, defaultShieldSeconds))
	case decision.InterventionToughnessBoost: // #1129, shadow-only
		return w.putPlayer(ctx, d, flagToughnessBoostSeconds, player, w.toughnessBoostOr(d.Value, defaultToughnessBoost))
	case decision.InterventionSoftPenalty: // #1134, shadow-only
		return w.putPlayer(ctx, d, flagSoftPenaltyAction, player, w.softPenaltyOr(d.Value, defaultSoftPenalty))
	}
	return fmt.Errorf("unknown intervention %q", d.Intervention)
}

func (w *Writer) put(ctx context.Context, d decision.Decision, flag string, scope flagservice.Scope, value any) error {
	_, err := w.svc.Put(ctx, FlagSetInterventions, flag, flagservice.Write{
		Value: value, Scope: scope, Writer: writerLabel, Reason: d.Intervention,
	})
	return err
}

func (w *Writer) putPlayer(ctx context.Context, d decision.Decision, flag string, scope flagservice.Scope, value any) error {
	if scope.Serial == "" {
		return fmt.Errorf("targeted intervention on %q requires a target serial", flag)
	}
	return w.put(ctx, d, flag, scope, value)
}

func (w *Writer) post(ctx context.Context, d decision.Decision, flag string, payload map[string]any) error {
	if s, ok := payload["serial"]; ok && s == "" && flag != flagControllerEffect {
		return fmt.Errorf("one-shot intervention on %q requires a target serial", flag)
	}
	_, err := w.svc.Post(ctx, FlagSetInterventions, flag, flagservice.Event{
		Scope: flagservice.Scope{GameID: d.GameID}, Payload: payload,
		Writer: writerLabel, Reason: d.Intervention,
	})
	return err
}

// toughnessBoostOr validates a toughness_boost directive "<seconds>" or
// "<seconds>:<boost>" (#1129) and returns it, or def when v is empty/malformed/
// out-of-bounds. Defensive sibling of tempoRampOr: the game-side parser
// (lifecycle_handlers.parse_toughness_boost_value) independently re-validates and
// no-ops on garbage, but validating here keeps a malformed LLM value from ever
// reaching the flag file. Bounds mirror the game side: seconds > 0 (capped),
// boost in [1.0, 2.0] (default 2.0 when omitted).
func (w *Writer) toughnessBoostOr(v, def string) string {
	if v == "" {
		return def
	}
	parts := strings.Split(strings.TrimSpace(v), ":")
	if len(parts) < 1 || len(parts) > 2 {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	seconds, serr := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	if serr != nil || seconds <= 0 || seconds > toughnessBoostMaxSeconds {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	if len(parts) == 2 {
		boost, berr := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
		if berr != nil || boost < toughnessBoostFactorMin || boost > toughnessBoostFactorMax {
			w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
			return def
		}
	}
	return v
}

// softPenaltyOr validates a soft_penalty directive "warn" | "tighten" |
// "tighten:<seconds>:<factor>" (#1134) and returns it, or def ("warn") when v is
// empty/malformed/out-of-bounds. Defensive sibling of toughnessBoostOr: the
// game-side parser (lifecycle_handlers.parse_soft_penalty_value) independently
// re-validates, but validating here keeps a malformed LLM value from ever
// reaching the flag file and — critically — never lets garbage escalate into a
// tighten (an unknown value degrades to the gentlest "warn"). Bounds mirror the
// game side: tighten seconds > 0 (capped), factor in [0.5, 1.0].
func (w *Writer) softPenaltyOr(v, def string) string {
	if v == "" {
		return def
	}
	t := strings.ToLower(strings.TrimSpace(v))
	if t == "warn" {
		return v
	}
	parts := strings.Split(t, ":")
	if parts[0] != "tighten" {
		// Unknown action: degrade to the safe default rather than dispatch garbage.
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	// Bare "tighten" uses game-side defaults; only the 3-field form carries bounds.
	if len(parts) == 1 {
		return v
	}
	if len(parts) != 3 {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	seconds, serr := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if serr != nil || seconds <= 0 || seconds > softPenaltyMaxSeconds {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	factor, ferr := strconv.ParseFloat(strings.TrimSpace(parts[2]), 64)
	if ferr != nil || factor < softPenaltyFactorMin || factor > softPenaltyFactorMax {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	return v
}

// autoRubberbandOr validates an auto_rubberband strength "gentle" | "strong"
// (#1143) and returns it, or def when v is empty. ANY other value (including a
// malformed one) degrades to the neutral "off" no-op rather than dispatching
// garbage — critically, an unknown value never escalates to "strong". Defensive
// sibling of softPenaltyOr: the game-side parser
// (lifecycle_handlers.parse_auto_rubberband_value) independently re-validates and
// no-ops on garbage, but validating here keeps a malformed LLM value from ever
// reaching the flag file.
func (w *Writer) autoRubberbandOr(v, def string) string {
	if v == "" {
		return def
	}
	t := strings.ToLower(strings.TrimSpace(v))
	if t == "gentle" || t == "strong" || t == "off" {
		return t
	}
	// Unknown strength: degrade to the safe neutral "off" no-op.
	w.log.Warn("agent.action_bad_value", "value", v, "fallback", "off")
	return "off"
}

// payloadOr returns v, or the default when v is empty.
func (w *Writer) payloadOr(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

// tempoRampOr validates a ramp_tempo directive "<target>:<seconds>:<curve>" and
// returns it, or def when v is empty/malformed/out-of-bounds (#1117). It is a
// defensive sibling of numOr: the game-side parser (base.py parse_ramp_tempo_value)
// independently validates again and no-ops on garbage, but validating here keeps a
// malformed LLM value from ever being written to the flag file. Bounds mirror
// base.py: target in [1.0, 1.3], seconds > 0 (capped), curve in {linear, ease}.
func (w *Writer) tempoRampOr(v, def string) string {
	if v == "" {
		return def
	}
	parts := strings.Split(strings.TrimSpace(v), ":")
	if len(parts) != 3 {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
		return def
	}
	target, terr := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	seconds, serr := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	curve := strings.ToLower(strings.TrimSpace(parts[2]))
	if terr != nil || serr != nil || !validTempoRampCurves[curve] ||
		seconds <= 0 || target < 1.0 || target > 1.3 || seconds > tempoRampMaxSeconds {
		w.log.Warn("agent.action_bad_value", "value", v, "fallback", def)
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
