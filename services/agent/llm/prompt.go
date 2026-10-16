// Package llm holds the prompt carrier and the prompt builders/decoders for the
// agent's OFFLINE LLM seams: the post-game retrospective (retro.go) and the
// shadow-experiment proposer (experiment/proposer.go, which reuses Prompt).
// In-game decisions use the typed decide contract (decide/llmio) instead; the
// legacy in-game prompt builder (Build, prompt_variant, operator note) was
// retired in #1234.
//
// prompt.go keeps the shared game-mechanics text (physicalModel, mode
// fragments) and the deterministic rendering helpers the retro uses.
// Determinism contract (relied on by the golden tests):
//   - No wall-clock reads — Now is injected.
//   - No randomness.
//   - No map-iteration-order dependence (players are sorted).
package llm

import (
	"strconv"
	"strings"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// Prompt is a System/User prompt pair for one text-generation call, plus the
// model it is attributed to. Backends (inference.Claude) send System and User and
// use their own configured model; Model is attribution only.
type Prompt struct {
	System string
	User   string
	Model  string
}

// unknown is the literal rendered for any never-observed (nil) signal.
const unknown = "unknown"

// physicalModel is the HOW-THE-GAME-PHYSICALLY-WORKS block (#1091), injected into
// the retro System prompt (#1158). The agent's first real gemma4
// proposal invented a mechanic FFA does not have ("utilize downed opponents")
// purely because the prompt never explained the physics; this block states the
// rules the model must reason within. The facts are verified against
// services/game_coordinator/games/base.py: motion-controlled death via an EMA of
// acceleration magnitude vs a tempo-interpolated threshold
// (threshold = lerp(SLOW[sens], FAST[sens], music_speed)), a ~0.3s startup grace
// (base.py grace_until = time.time() + 0.3), and movement-control-vs-shared-tempo
// competition with no health/damage/direct combat. Kept terse — the prompt budget
// is already large.
const physicalModel = `HOW THE GAME PHYSICALLY WORKS:
  - Each player holds a PS Move controller that streams its acceleration
    magnitude — a movement-intensity reading. There is NO health, NO damage,
    and players do NOT attack each other; they compete only on movement control.
  - A player is eliminated when their (smoothed) movement intensity exceeds the
    current effective death threshold. Hold still enough and you survive.
  - Music tempo / game speed sets how much movement is tolerated: the effective
    threshold is interpolated by tempo. SLOW tempo => low threshold (move gently
    or you're out); FAST tempo => higher threshold (more movement allowed). The
    User snapshot's music_tempo + death_threshold and the game-speed track are
    this reference frame — read each player's intensity against them.
  - A brief ~0.3s startup grace at spawn keeps initial spikes from insta-killing.`

// modeFragments maps a NORMALIZED game-mode key (see normalizeMode) to its
// mode-specific rules block (#1091). Only FFA is fully written today — it is the
// one mode we test/run — and it explicitly corrects the LLM's hallucinated
// "downed opponents" by stating elimination is PERMANENT with no respawn/downed
// state. Other modes get a one-line stub so the prompt is honest about what is
// known. An unrecognized/empty mode falls back to genericModeFragment via
// modePromptFragment. Facts verified: FFA = last-player-standing permanent
// elimination (ffa.py "Players stay dead permanently"; revive_player is DENIED in
// FFA per interventions.py MODE_CAPABILITY_DENY). death_grace_period_seconds is
// NOT presented as an FFA lever — it governs post-DEATH respawn grace, which FFA
// has no use for (#1090).
var modeFragments = map[string]string{
	"ffa": `GAME MODE — FFA (Free-For-All), last player standing:
  - ELIMINATION IS PERMANENT. When a player crosses the tempo-scaled threshold
    they are out for the rest of the game — there is NO respawn, NO revive, and
    NO "downed" or "dead-player" state to interact with. Do not reason about
    reviving, reusing, or re-engaging eliminated players; they are simply gone.
  - The round ends when one player remains (the winner) — or all remaining
    players are eliminated together.
  - Effective FFA levers (only those also in the allow-list below may be used):
    pace the difficulty for the whole room by shifting music tempo / game
    speed (which moves everyone's threshold), nudge global or per-player
    sensitivity, grant a short per-player movement-grace shield to a player about
    to be unfairly eliminated, and play audio cues / controller effects to steer
    the room. note: death_grace_period_seconds governs post-death respawn grace
    and has NO effect in FFA (no respawns) — it is not a lever here.
  - set_player_handicap: per-player multiplier on that player's death threshold
    (>1 = higher threshold = harder to eliminate / "help"; <1 = lower = easier /
    "rein in"), clamped 0.5-2.0; COMPOSES WITH (does not replace) per-player
    sensitivity, so use it for continuous rubber-banding toward fairness.`,
	"zombie":     `GAME MODE — Zombie (role-based, respawn): humans vs zombies; a tagged human becomes a zombie and respawn/role-change exists, so eliminated players CAN re-enter as the other role. (Detailed levers not yet specified — reason conservatively within the physics above.)`,
	"swapper":    `GAME MODE — Swapper (role/team-swap, respawn): players swap roles/teams during play and respawn exists, so an eliminated player is not necessarily permanently out. (Detailed levers not yet specified — reason conservatively within the physics above.)`,
	"tournament": `GAME MODE — Tournament (bracket): players advance through a bracket of matches rather than one free-for-all; elimination is per-match. (Detailed levers not yet specified — reason conservatively within the physics above.)`,
}

// genericModeFragment is the fallback when the current mode is empty/unknown
// (#1091): it commits to no mode-specific rule beyond the shared physics, so the
// model never invents mode mechanics it was not told about.
const genericModeFragment = `GAME MODE — not specified this cycle: reason only from the shared physics above
  and the live snapshot; do not assume mode-specific mechanics (respawn, roles,
  brackets) that are not stated.`

// normalizeMode maps a raw game-mode string to a stable fragment key (#1091). The
// mode reaches the agent from two sources that disagree on casing/wording:
// game_coordinator's get_game_name() ("FFA", "Zombie", "Swapper", "Tournament",
// "Nonstop Joust") and menu aliases ("ffa", "joust", "free for all"). We lower-case
// and fold the known FFA aliases onto "ffa" so all of them select the FFA block;
// other recognized modes map to their key; anything else returns "" (=> generic).
func normalizeMode(raw string) string {
	m := strings.ToLower(strings.TrimSpace(raw))
	switch m {
	case "ffa", "joust", "joust ffa", "free for all", "free-for-all":
		return "ffa"
	case "zombie", "zombies":
		return "zombie"
	case "swapper":
		return "swapper"
	case "tournament":
		return "tournament"
	default:
		return ""
	}
}

// modePromptFragment returns the mode-specific rules block for the current game
// mode (#1091), or the generic fallback when the mode is empty/unrecognized. It is
// the single selector composed into the System prompt, so adding a mode means one
// modeFragments entry + (if needed) a normalizeMode alias.
func modePromptFragment(rawMode string) string {
	if frag, ok := modeFragments[normalizeMode(rawMode)]; ok {
		return frag
	}
	return genericModeFragment
}

// joinInterventions renders the allow-list as a ", "-joined string, or the
// "(none)" marker when empty (the fail-closed default dispatches nothing).
func joinInterventions(allowed []string) string {
	if len(allowed) == 0 {
		return "(none)"
	}
	return strings.Join(allowed, ", ")
}

// writeSpeedTrack renders the session game-speed (tempo) sparkline (#1082): the
// reference frame the per-player tracks are read against. The track ramps over the
// game (accelerate rules / adjust_music_tempo step it), so it renders as a rising
// sparkline. Omitted entirely when no tempo was ever observed (keeps the no-signal
// prompt byte-identical to the pre-#1082 output for that section).
func writeSpeedTrack(b *strings.Builder, ctx gamecontext.GameContext) {
	spark := gamecontext.RenderSpeedTrack(ctx.SpeedTrack)
	if spark == "" {
		return
	}
	b.WriteString("\nGame speed (tempo over the game; oldest first):\n")
	b.WriteString("  speed: ")
	b.WriteString(spark)
	b.WriteString("\n")
}

// writeTimeline renders the compact RECENT EVENTS section shared by the in-game
// prompt and the retro (#916): the bounded rolling narrative the GameContext now
// carries (state deltas, eliminations, phase transitions), one line per event in
// oldest-first order with a relative age. An empty timeline renders "(none)" so
// the section is always present and deterministic — the prompt never reads the
// wall clock; `now` (RenderTimeline's reference) is the injected BuildInput.Now.
func writeTimeline(b *strings.Builder, ctx gamecontext.GameContext, now time.Time) {
	b.WriteString("\nRecent events (oldest first; age relative to captured_at):\n")
	lines := gamecontext.RenderTimeline(ctx.Timeline, now)
	if len(lines) == 0 {
		b.WriteString("  (none)\n")
		return
	}
	for _, line := range lines {
		b.WriteString("  ")
		b.WriteString(line)
		b.WriteString("\n")
	}
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

// stringOrUnknown renders a *string as its value, or "unknown" when nil.
func stringOrUnknown(v *string) string {
	if v == nil {
		return unknown
	}
	return *v
}

// gameKindOrUnknown renders GameContext.GameKind ("real"/"shadow") as-is, or
// "unknown" when empty (never observed on a labeled signal). GameKind is a plain
// string (not a pointer), so "" is the unobserved sentinel (#845).
func gameKindOrUnknown(kind string) string {
	if kind == "" {
		return unknown
	}
	return kind
}
