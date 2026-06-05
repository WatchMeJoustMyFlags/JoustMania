// Package llm builds the prompt the agent would send to an LLM on the M4
// (#739) llm-mode decision path.
//
// Build is a pure, deterministic function: it serializes the agent's
// per-cycle game context and flag snapshot into a System/User prompt pair. The
// llm path itself is not wired yet — this package exists so the decision loop
// can construct, and a follow-up PR (PR 2/2 of the M4 prompt-capture spike) can
// CAPTURE-ON-TELEMETRY, the exact prompt the agent would have sent every
// llm-mode cycle. Nothing imports this package today, so it is behavior-neutral.
//
// Determinism contract (relied on by the golden tests and by capture):
//   - No wall-clock reads — BuildInput.Now is injected.
//   - No randomness.
//   - No map-iteration-order dependence (objectives and players are sorted).
//
// The System prompt's RESPONSE CONTRACT mirrors the fields of
// decision.Decision (intervention, target_serial, value, reason,
// objective_served) so an llm reply can be unmarshaled straight into a Decision.
package llm

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// Prompt is the System/User prompt pair the agent would send for one cycle,
// plus attribution for the capability flags that produced it.
type Prompt struct {
	System  string // role + objectives + allowed interventions + response contract
	User    string // serialized game snapshot for this cycle
	Variant string // resolved prompt_variant that produced it
	Model   string // capability model flag (attribution only)
}

// BuildInput is everything Build needs to render a Prompt for one cycle.
type BuildInput struct {
	Snapshot flags.Snapshot
	Context  gamecontext.GameContext
	Now      time.Time // injected for determinism — Build must never read the wall clock
}

// unknown is the literal rendered for any never-observed (nil) signal.
const unknown = "unknown"

// variantGuidance maps a resolved prompt_variant to its System-prompt guidance
// line. This is the single producer of variant text so #740 can expand it in
// one place. conservativeVariant is the default and the fallback for any
// unrecognized variant value.
const (
	conservativeVariant = "conservative"
	aggressiveVariant   = "aggressive"
	balancedVariant     = "balanced"
)

var variantGuidance = map[string]string{
	conservativeVariant: `Intervene only when a signal is clearly actionable; when in doubt, choose "noop". Favor ambient cues over state changes.`,
	aggressiveVariant:   "Intervene proactively to maximize excitement; prefer state-changing interventions when the objective calls for it.",
	balancedVariant:     "Intervene when a signal is moderately actionable; balance ambient cues and state changes.",
}

// resolveVariant maps a raw prompt_variant flag value to a known variant,
// falling back to conservative for anything unrecognized.
func resolveVariant(raw string) string {
	if _, ok := variantGuidance[raw]; ok {
		return raw
	}
	return conservativeVariant
}

// Build renders the deterministic prompt for one decision cycle. The same
// BuildInput (with a fixed Now) always yields a byte-identical Prompt.
func Build(in BuildInput) Prompt {
	variant := resolveVariant(in.Snapshot.Capability.PromptVariant)
	return Prompt{
		System:  buildSystem(in.Snapshot, variant),
		User:    buildUser(in.Context, in.Now),
		Variant: variant,
		Model:   in.Snapshot.Capability.Model,
	}
}

// buildSystem renders the System prompt: role, objectives, allow-list, policy
// constraints, the resolved variant guidance, and the JSON response contract.
func buildSystem(s flags.Snapshot, variant string) string {
	var b strings.Builder
	b.WriteString(`You are the JoustMania game director, an autonomous agent that tunes a live
physical movement game to make it more fun. Each decision cycle you receive a
snapshot of the game and must choose AT MOST ONE intervention (or none).

OBJECTIVES (weights; higher = more important this session):
  `)
	b.WriteString(summarizeObjectives(s.Objectives))
	b.WriteString(`

You may ONLY choose interventions from this allow-list. Anything else is rejected:
  `)
	b.WriteString(joinInterventions(s.InterventionsAllowed))
	b.WriteString(`

POLICY CONSTRAINTS:
  - Player-targeted interventions are blocked when the target's battery < `)
	b.WriteString(strconv.Itoa(s.Policy.BatteryThreshold))
	b.WriteString(`%.
  - At most `)
	b.WriteString(strconv.Itoa(s.Policy.MaxInterventionsPerMinute))
	b.WriteString(` weighted interventions per minute across the session.
  - Prefer the least disruptive intervention that serves the objective.

VARIANT: `)
	b.WriteString(variant)
	b.WriteString(". ")
	b.WriteString(variantGuidance[variant])
	b.WriteString(`

RESPONSE CONTRACT — reply with EXACTLY ONE JSON object, no prose, matching:
{
  "intervention": "<one of the allow-list, or \"noop\">",
  "target_serial": "<player serial, or \"\" for session-scoped>",
  "value": "<intervention payload as a string, or \"\" for the default>",
  "reason": "<one short sentence explaining the choice>",
  "objective_served": "<one of: endurance, balanced, accelerate, chaos>"
}
If no intervention is warranted, return intervention="noop" with a reason.`)
	return b.String()
}

// summarizeObjectives renders the objective weights as a sorted "k=v" list using
// the same ordering convention as decision.summarizeObjectives, joined with
// ", " (e.g. "balanced=0.7, chaos=0.1, endurance=0.1"). Weights are formatted
// with the 'g' verb so they read the way the flag declares them.
func summarizeObjectives(weights map[string]float64) string {
	parts := make([]string, 0, len(weights))
	for k, v := range weights {
		parts = append(parts, k+"="+strconv.FormatFloat(v, 'g', -1, 64))
	}
	sort.Strings(parts)
	return strings.Join(parts, ", ")
}

// joinInterventions renders the allow-list as a ", "-joined string, or the
// "(none)" marker when empty (the fail-closed default dispatches nothing).
func joinInterventions(allowed []string) string {
	if len(allowed) == 0 {
		return "(none)"
	}
	return strings.Join(allowed, ", ")
}

// buildUser renders the variant-independent User message: the session header,
// the session signals, and the per-player signals sorted by serial. Now is
// rendered as the captured_at timestamp in UTC RFC3339.
func buildUser(ctx gamecontext.GameContext, now time.Time) string {
	var b strings.Builder
	fmt.Fprintf(&b, "GAME SNAPSHOT (session=%s, mode=%s, captured_at=%s)\n",
		ctx.SessionID, stringOrUnknown(ctx.Session.GameMode), now.UTC().Format(time.RFC3339))

	b.WriteString("Session:\n")
	fmt.Fprintf(&b, "  duration_seconds: %s\n", floatPtrOrUnknown(ctx.Session.DurationSeconds))
	fmt.Fprintf(&b, "  active_players: %s\n", intPtrOrUnknown(ctx.Session.ActivePlayerCount))
	fmt.Fprintf(&b, "  game_active: %s\n", boolPtrOrUnknown(ctx.Session.GameActive))
	fmt.Fprintf(&b, "  elimination_sequence: [%s]\n", strings.Join(ctx.Session.EliminationSequence, ", "))

	b.WriteString("\nPlayers (sorted by serial; \"unknown\" = signal never observed):\n")
	if len(ctx.Players) == 0 {
		b.WriteString("  (none)")
		return b.String()
	}

	serials := make([]string, 0, len(ctx.Players))
	for serial := range ctx.Players {
		serials = append(serials, serial)
	}
	sort.Strings(serials)

	lines := make([]string, 0, len(serials))
	for _, serial := range serials {
		p := ctx.Players[serial]
		lines = append(lines, fmt.Sprintf(
			"  %s: active=%s movement_intensity=%s movement_variance=%s battery_pct=%s skill=%s",
			serial,
			boolPtrOrUnknown(p.Active),
			floatPtrOrUnknown(p.MovementIntensity),
			floatPtrOrUnknown(p.MovementVariance),
			floatPtrOrUnknown(p.BatteryPct),
			floatPtrOrUnknown(p.SkillLevel),
		))
	}
	b.WriteString(strings.Join(lines, "\n"))
	return b.String()
}

// floatPtrOrUnknown renders a *float64 as a 2-decimal fixed string, or the
// "unknown" literal when nil. A non-nil pointer at 0 renders "0.00".
func floatPtrOrUnknown(v *float64) string {
	if v == nil {
		return unknown
	}
	return strconv.FormatFloat(*v, 'f', 2, 64)
}

// intPtrOrUnknown renders a *int as its decimal value, or "unknown" when nil.
func intPtrOrUnknown(v *int) string {
	if v == nil {
		return unknown
	}
	return strconv.Itoa(*v)
}

// boolPtrOrUnknown renders a *bool as "true"/"false", or "unknown" when nil.
func boolPtrOrUnknown(v *bool) string {
	if v == nil {
		return unknown
	}
	return strconv.FormatBool(*v)
}

// stringOrUnknown renders a *string as its value, or "unknown" when nil.
func stringOrUnknown(v *string) string {
	if v == nil {
		return unknown
	}
	return *v
}
