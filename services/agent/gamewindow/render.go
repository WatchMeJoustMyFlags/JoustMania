package gamewindow

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/joustmania/agent/gamesummary"
)

// blockHeader labels the cross-game context section in the prompt. It is a fixed,
// greppable marker (#929 acceptance: "each LLM call includes the last N game
// summaries as a narrative context block") so the block is findable in a captured
// prompt and in a Jaeger llm.prompt.system attribute by name, independent of how
// many games it holds.
const blockHeader = "PRIOR GAMES (most recent last; cross-game memory):"

// emptyMarker is rendered when the window is empty (no games have ended yet, or N
// resolved to 0). The section is ALWAYS present and ALWAYS greppable — an empty
// window renders an explicit marker rather than omitting the section — so the
// prompt shape is deterministic and a reader can tell "no prior games" from "the
// block was dropped". Mirrors the prompt builder's "(none)" timeline convention.
const emptyMarker = "  (no prior games)"

// Render assembles the compact, deterministic NARRATIVE CONTEXT BLOCK from the
// given summaries — the last N the decision path pulled via Store.Recent(N) (#929
// M7-2). It is the cross-game memory injected into every llm prompt. The block is:
//
//   - COMPACT, not the full JSON: each game renders to ONE summary line plus at
//     most a couple of detail lines (dominance, notable interventions), so N games
//     stay a few lines each — the model gets the narrative, not a telemetry dump
//     (the full Summary lives in the on-disk file / the agent.game.summary span).
//   - DETERMINISTic: pure over its input (no clock/random/map-iteration order —
//     the Summary's own slices are already serial-/order-sorted by the M7-1
//     builder), so the same window always renders byte-identically. The golden
//     prompt tests rely on this.
//   - ALWAYS PRESENT: an empty slice renders the header + the explicit
//     "(no prior games)" marker, so the section never vanishes.
//
// summaries are oldest-first (Store.Recent's contract); they render in that order
// under "most recent last" so the model reads them chronologically toward the
// present game.
func Render(summaries []gamesummary.Summary) string {
	var b strings.Builder
	b.WriteString(blockHeader)
	b.WriteString("\n")
	if len(summaries) == 0 {
		b.WriteString(emptyMarker)
		return b.String()
	}
	for i, s := range summaries {
		renderSummary(&b, i+1, s)
	}
	// Trim the trailing newline so the block has no dangling blank line — a stable,
	// minimal shape for the golden tests and the prompt concatenation.
	return strings.TrimRight(b.String(), "\n")
}

// renderSummary writes one game's compact lines: a header line with the at-a-glance
// counts, then optional dominance and intervention-outcome lines only when they
// carry signal. idx is the 1-based position in the rendered window (for a stable,
// human-readable ordinal — NOT the game id, which is high-cardinality and not
// useful to the model's cross-game reasoning).
func renderSummary(b *strings.Builder, idx int, s gamesummary.Summary) {
	// Header: kind, mode, duration, players, eliminations, interventions. These are
	// the narrative skeleton — "a 92s real joust game with 4 players, 3 eliminated,
	// 2 agent interventions" — enough for the model to place the game without the
	// per-player arcs (which would blow the budget for N games).
	fmt.Fprintf(b, "  %d. %s/%s duration=%s players=%d eliminations=%d interventions=%d\n",
		idx,
		kindOrUnknown(s.GameKind),
		modeOrUnknown(s.GameMode),
		durationOrUnknown(s.DurationSeconds),
		s.PlayerCount,
		s.EliminationCount,
		s.InterventionCount,
	)

	// Dominance, only when a player clearly ran away with the game — the single most
	// narrative-worthy outcome (#928 dominant-player). Named heuristic + margin so the
	// model sees WHY, mirroring the on-disk field's transparency.
	if s.Dominance.Dominated && s.Dominance.Serial != "" {
		fmt.Fprintf(b, "     dominated_by=%s (%s, margin=%s)\n",
			s.Dominance.Serial,
			s.Dominance.Heuristic,
			strconv.FormatFloat(s.Dominance.Margin, 'f', 1, 64),
		)
	}

	// Intervention outcomes, only when any fired — the agent's OWN history in that
	// game (what it did and whether it landed), the most relevant cross-game signal
	// for a director learning from its past actions. Rendered compactly: the action,
	// dispatched-or-blocked, and the best-effort engagement effect when known.
	for _, iv := range s.Interventions {
		fmt.Fprintf(b, "     intervention=%s%s %s%s\n",
			iv.Intervention,
			targetSuffix(iv.TargetSerial),
			dispatchWord(iv.Dispatched),
			effectSuffix(iv.Effect),
		)
	}
}

// targetSuffix renders "->serial" for a player-targeted intervention, or "" for a
// session-scoped one, so a session-scoped action reads cleanly.
func targetSuffix(serial string) string {
	if serial == "" {
		return ""
	}
	return "->" + serial
}

// dispatchWord renders the intervention's gate outcome as a single word the model
// can reason over: it reached the action sink, or a permission/policy gate blocked
// it.
func dispatchWord(dispatched bool) string {
	if dispatched {
		return "dispatched"
	}
	return "blocked"
}

// effectSuffix renders the best-effort before/after engagement effect (#928's
// coarse correlation) when the timeline had comparable samples on both sides, or
// "" when it did not — the model sees "this action moved intensity by +0.12" only
// when there is an honest number to show.
func effectSuffix(eff *gamesummary.InterventionEffect) string {
	if eff == nil {
		return ""
	}
	parts := make([]string, 0, 2)
	if eff.MeanIntensityDelta != nil {
		parts = append(parts, "intensity_delta="+strconv.FormatFloat(*eff.MeanIntensityDelta, 'f', 2, 64))
	}
	if eff.ActivePlayersDelta != nil {
		parts = append(parts, "active_delta="+strconv.Itoa(*eff.ActivePlayersDelta))
	}
	if len(parts) == 0 {
		return ""
	}
	return " [" + strings.Join(parts, " ") + "]"
}

// kindOrUnknown / modeOrUnknown / durationOrUnknown mirror the prompt builder's
// "unknown" sentinel convention (llm/prompt.go) so the cross-game block reads in
// the same vocabulary as the rest of the prompt — an unobserved field is the
// literal "unknown", never an empty gap.
func kindOrUnknown(kind string) string {
	if kind == "" {
		return "unknown"
	}
	return kind
}

func modeOrUnknown(mode string) string {
	if mode == "" {
		return "unknown"
	}
	return mode
}

func durationOrUnknown(d *float64) string {
	if d == nil {
		return "unknown"
	}
	return strconv.FormatFloat(*d, 'f', 1, 64) + "s"
}
