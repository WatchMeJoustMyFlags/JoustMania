package llm

// retro.go builds the POST-GAME retrospective prompt (#844): the prompt the
// agent would send to an offline LLM analyst when a game ENDS, asking it to
// suggest calibration tweaks for the NEXT game. It is the sibling of the in-game
// builder in prompt.go and shares its helpers (floatPtrOrUnknown,
// summarizeObjectives, joinInterventions, stringOrUnknown, the `unknown`
// literal) and its determinism contract:
//
//   - No wall-clock reads — RetroInput.Now is injected.
//   - No randomness.
//   - Players are sorted by serial; no map-iteration-order dependence.
//   - Floats render at fixed precision (floatPtrOrUnknown's 2 decimals).
//
// The same RetroInput (with a fixed Now) always yields a byte-identical
// RetroPrompt. Unlike Build, there is NO Variant: the offline analyst makes a
// handful of human-reviewed recommendations, so there is no in-game
// aggressiveness dial.
//
// Capture-first, like #739: nothing calls a backend yet. decision/retro_capture.go
// records the built prompt on an `agent.llm.retro` span for offline replay; a
// follow-up wires a real backend once the fallback chain (#741) exists.

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// RetroPrompt is the System/User prompt pair the agent would send to the offline
// analyst at game end, plus the capability model flag for attribution. There is
// deliberately no Variant: a post-game analyst has no in-game aggressiveness
// variant.
type RetroPrompt struct {
	System string // role + calibration surface + policy + response contract
	User   string // serialized full-session summary
	Model  string // capability model flag (attribution only)
}

// RetroInput is everything BuildRetro needs to render a RetroPrompt at game end.
type RetroInput struct {
	Snapshot flags.Snapshot
	Context  gamecontext.GameContext
	Now      time.Time // injected for determinism — BuildRetro must never read the wall clock
}

// Calibration-surface flag names the analyst may suggest. These are the #766
// calibration flags read at game INIT (docs/research/722-intervention-surface.md
// §11) — NOT the in-game intervention allow-list. They are listed here once so
// the System prompt and the contract-presence test share a single source.
const (
	calibDifficultyFactor = "global_difficulty_factor"
	calibPacingProfile    = "pacing_profile"
	calibThresholdTable   = "threshold_table"
	calibObjectiveVariant = "objective_variant"
)

// BuildRetro renders the deterministic retrospective prompt for one finished
// session. The same RetroInput (with a fixed Now) always yields a byte-identical
// RetroPrompt.
func BuildRetro(in RetroInput) RetroPrompt {
	return RetroPrompt{
		System: buildRetroSystem(),
		User:   buildRetroUser(in.Context, in.Now),
		Model:  in.Snapshot.Capability.Model,
	}
}

// buildRetroSystem renders the System prompt: the post-game analyst role, the
// calibration surface as the suggestion vocabulary, the smallest-change policy,
// and the JSON response contract. It takes no input — the calibration surface is
// fixed, so the System prompt is constant (the session evidence lives in User).
func buildRetroSystem() string {
	return `You are the JoustMania post-game analyst. A physical movement party game has just
ended. You receive a full session summary and must suggest CALIBRATION TWEAKS that
would make the NEXT game more fun. You are NOT controlling a live game — you make
at most a handful of recommendations, which a human reviews. Suggestions are
RECORDED ONLY and never auto-applied.

CALIBRATION SURFACE — you may ONLY suggest changes to these flags. Each is read at
game INIT:

  ` + calibDifficultyFactor + `   (float, ~0.5..1.5; 1.0 = current) — scales overall
                             movement demand for the next game.
  ` + calibPacingProfile + `             (enum: "relaxed" | "standard" | "intense") — the
                             music-schedule preset that shapes round tempo.
  ` + calibThresholdTable + `            (named death/warning threshold table, e.g.
                             "easy" | "standard" | "hard").
  ` + calibObjectiveVariant + `          (enum: endurance | balanced | accelerate | chaos) —
                             the session goal weighting for the next game.

POLICY: suggest the SMALLEST change that addresses an observed problem. If the
session looked healthy, return an empty suggestions list. Do not invent flags
outside the calibration surface.

RESPONSE CONTRACT — reply with EXACTLY ONE JSON object, no prose, matching:
{
  "session_assessment": "<one short sentence: how did this game go?>",
  "suggestions": [
    {
      "flag": "<one of: ` + calibDifficultyFactor + `, ` + calibPacingProfile + `, ` + calibThresholdTable + `, ` + calibObjectiveVariant + `>",
      "value": "<the suggested value as a string>",
      "reason": "<one short sentence tying the change to session evidence>"
    }
  ]
}
If no tweak is warranted, return "suggestions": [].`
}

// buildRetroUser renders the User message: the session header, the derived
// outcome (duration, final active players, elimination order, winner), and the
// per-player end-state sorted by serial. Now is rendered as the captured_at
// timestamp in UTC RFC3339.
func buildRetroUser(ctx gamecontext.GameContext, now time.Time) string {
	var b strings.Builder
	fmt.Fprintf(&b, "SESSION RETROSPECTIVE (session=%s, kind=%s, mode=%s, captured_at=%s)\n",
		ctx.SessionID, gameKindOrUnknown(ctx.GameKind), stringOrUnknown(ctx.Session.GameMode), now.UTC().Format(time.RFC3339))

	serials := sortedSerials(ctx.Players)
	winner := winnerSerial(serials, ctx.Session.EliminationSequence)

	b.WriteString("\nOutcome:\n")
	fmt.Fprintf(&b, "  duration_seconds: %s\n", floatPtrOrUnknown(ctx.Session.DurationSeconds))
	fmt.Fprintf(&b, "  final_active_players: %s\n", intPtrOrUnknown(ctx.Session.ActivePlayerCount))
	fmt.Fprintf(&b, "  elimination_order: [%s]\n", strings.Join(ctx.Session.EliminationSequence, ", "))
	fmt.Fprintf(&b, "  winner: %s\n", winner)

	b.WriteString("\nPlayers (sorted by serial; \"unknown\" = signal never observed):\n")
	if len(ctx.Players) == 0 {
		b.WriteString("  (none)")
		return b.String()
	}

	lines := make([]string, 0, len(serials))
	for _, serial := range serials {
		p := ctx.Players[serial]
		lines = append(lines, fmt.Sprintf(
			"  %s: outcome=%s battery_pct=%s skill=%s movement_intensity=%s movement_variance=%s",
			serial,
			playerOutcome(serial, ctx.Session.EliminationSequence),
			floatPtrOrUnknown(p.BatteryPct),
			floatPtrOrUnknown(p.SkillLevel),
			floatPtrOrUnknown(p.MovementIntensity),
			floatPtrOrUnknown(p.MovementVariance),
		))
	}
	b.WriteString(strings.Join(lines, "\n"))
	return b.String()
}

// sortedSerials returns the player serials sorted ascending. Sorting (not map
// order) is what makes the prompt deterministic.
func sortedSerials(players map[string]*gamecontext.PlayerSignals) []string {
	serials := make([]string, 0, len(players))
	for serial := range players {
		serials = append(serials, serial)
	}
	sort.Strings(serials)
	return serials
}

// playerOutcome derives a player's end-of-game outcome from its position in the
// elimination sequence: the first serial eliminated is "eliminated #1", the
// second "eliminated #2", and so on. A serial absent from the sequence survived
// the game and renders "survivor".
func playerOutcome(serial string, eliminationSequence []string) string {
	for i, s := range eliminationSequence {
		if s == serial {
			return fmt.Sprintf("eliminated #%d", i+1)
		}
	}
	return "survivor"
}

// winnerSerial returns the sole survivor's serial — a player present in the
// roster but absent from the elimination sequence — when EXACTLY one player
// survived. Zero survivors (everyone eliminated) or more than one survivor both
// render the "unknown" literal: there is no single, unambiguous winner.
func winnerSerial(serials, eliminationSequence []string) string {
	eliminated := make(map[string]bool, len(eliminationSequence))
	for _, s := range eliminationSequence {
		eliminated[s] = true
	}
	var survivors []string
	for _, s := range serials {
		if !eliminated[s] {
			survivors = append(survivors, s)
		}
	}
	if len(survivors) == 1 {
		return survivors[0]
	}
	return unknown
}
