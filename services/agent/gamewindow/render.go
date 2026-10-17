package gamewindow

import (
	"fmt"
	"strconv"
	"strings"
)

// blockHeader labels the cross-game section of the proposer prompt. It is a fixed,
// greppable marker so the block is findable in a captured prompt.
const blockHeader = "PRIOR GAMES (real games only, most recent last; game-level aggregates, no player identity):"

// emptyMarker is rendered when no real game is in the window, so the section is
// always present and a reader can tell "no prior games" from "block dropped".
const emptyMarker = "  (no prior games)"

// linkedHeader labels a shadow game's own experiment history (UC-042 BR-005),
// rendered as a separate section so shadow evidence never reads as real play.
const linkedHeader = "LINKED SHADOW GAMES (this experiment only, most recent last; game-level aggregates):"

// patternHeader labels the pattern-driven rows of the linked set (UC-042 BR-006).
const patternHeader = "PATTERN-DRIVEN PLAYERS (this experiment only; synthetic controllers, no person behind them; keyed by archetype and slot):"

// Render assembles the PRIOR GAMES block from a window snapshot: the real-game
// section always, then, for a shadow game, its own experiment's linked set in
// separate sections. It is pure and deterministic. The input types carry no serial
// or name, so the block cannot contain one (UC-042 BR-004 to BR-006).
func Render(w Window) string {
	var b strings.Builder
	b.WriteString(blockHeader)
	b.WriteString("\n")
	if len(w.Games) == 0 {
		b.WriteString(emptyMarker)
		b.WriteString("\n")
	}
	for i, g := range w.Games {
		renderGame(&b, i+1, "", g)
	}
	if w.Linked != nil {
		renderLinked(&b, *w.Linked)
	}
	return strings.TrimRight(b.String(), "\n")
}

func renderLinked(b *strings.Builder, set LinkedSet) {
	b.WriteString(linkedHeader)
	b.WriteString("\n")
	if len(set.Games) == 0 {
		b.WriteString(emptyMarker)
		b.WriteString("\n")
	}
	for i, g := range set.Games {
		renderGame(b, i+1, "arm="+orUnknown(g.Arm)+" ", g.Game)
	}
	if len(set.PatternGames) == 0 {
		return
	}
	b.WriteString(patternHeader)
	b.WriteString("\n")
	for i, pg := range set.PatternGames {
		renderPatternGame(b, i+1, pg)
	}
}

func renderGame(b *strings.Builder, idx int, prefix string, g GameAggregate) {
	fmt.Fprintf(b, "  %d. %s%s duration=%s players=%d outcome=%s death_rate=%s near_death_rate=%s\n",
		idx,
		prefix,
		orUnknown(g.Mode),
		secondsOrUnknown(g.DurationSeconds),
		g.PlayerCount,
		orUnknown(g.Outcome),
		rateOrUnknown(g.DeathRate),
		rateOrUnknown(g.NearDeathRate),
	)
	for _, iv := range g.Interventions {
		fmt.Fprintf(b, "     intervention=%s dispatched=%d blocked=%d%s\n",
			iv.Type, iv.Dispatched, iv.Blocked, effectSuffix(iv))
	}
}

func renderPatternGame(b *strings.Builder, idx int, pg PatternGame) {
	fmt.Fprintf(b, "  %d. arm=%s pair=%s archetype=%s params=%s outcome=%s censored=%t\n",
		idx, orUnknown(pg.Arm), pairOrNone(pg.PairIndex), orUnknown(pg.Archetype), orUnknown(pg.ParamsDigest), orUnknown(pg.Outcome), pg.Censored)
	for _, p := range pg.Players {
		fmt.Fprintf(b, "     slot=%d %s survival=%s\n", p.Slot, placeWord(p), secondsOrUnknown(p.SurvivalSeconds))
	}
}

func placeWord(p PatternPlayer) string {
	switch {
	case p.Won:
		return "won"
	case p.EliminationPlace > 0:
		return "out=" + strconv.Itoa(p.EliminationPlace)
	default:
		return "survived"
	}
}

// effectSuffix renders the mean measured effect of an intervention type, or ""
// when nothing was measured.
func effectSuffix(iv InterventionAggregate) string {
	parts := make([]string, 0, 2)
	if iv.MeanIntensityDelta != nil {
		parts = append(parts, "mean_intensity_delta="+strconv.FormatFloat(*iv.MeanIntensityDelta, 'f', 2, 64))
	}
	if iv.MeanActiveDelta != nil {
		parts = append(parts, "mean_active_delta="+strconv.FormatFloat(*iv.MeanActiveDelta, 'f', 1, 64))
	}
	if len(parts) == 0 {
		return ""
	}
	return " [" + strings.Join(parts, " ") + "]"
}

func pairOrNone(i int) string {
	if i < 0 {
		return "none"
	}
	return strconv.Itoa(i)
}

func orUnknown(s string) string {
	if s == "" {
		return "unknown"
	}
	return s
}

func secondsOrUnknown(d *float64) string {
	if d == nil {
		return "unknown"
	}
	return strconv.FormatFloat(*d, 'f', 1, 64) + "s"
}

func rateOrUnknown(r *float64) string {
	if r == nil {
		return "unknown"
	}
	return strconv.FormatFloat(*r, 'f', 2, 64)
}
