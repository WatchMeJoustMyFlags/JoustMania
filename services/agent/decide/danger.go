package decide

import (
	"math"
	"strconv"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// danger.go derives the explicit danger signals of State (#1262): numeric
// comparisons done in Go and handed to every backend as a rank, a margin and a
// category, because a model reading "intensity 2.2" next to "death_threshold
// 2.30" in prose does not reliably notice the player is about to be eliminated
// (Jev 1.13 is documented as weak at numeric comparison; the LLMs benefit too).

// Danger categories of PlayerState.Danger. The empty string means unknown: the
// player is not alive, its intensity was never observed, or no positive death
// threshold has been observed for the session.
const (
	// DangerSafe: intensity below DangerElevatedRatio of the death threshold.
	DangerSafe = "safe"
	// DangerElevated: intensity at or above DangerElevatedRatio of the death
	// threshold, below DangerNearRatio.
	DangerElevated = "elevated"
	// DangerNearThreshold: intensity at or above DangerNearRatio of the death
	// threshold (including at or over it): one twitch from elimination.
	DangerNearThreshold = "near_threshold"
)

// Limits of the danger signals: they read the SESSION's effective death
// threshold (gamecontext.SessionSignals.DeathThreshold), not a per-player one.
// Per-player sensitivity, handicap factors and shields are invisible here, so a
// player with a protective factor or an active shield can still show
// near_threshold. PlayerState.Shielded flags a recent shield separately.

// Danger cut-offs, as intensity / effective death threshold.
//
// DangerNearRatio = 0.85 sits in the game's own warning band: the coordinator
// warns a player at SLOW_WARNING/FAST_WARNING, which is 0.68..0.92 of the
// matching death threshold across the five sensitivity levels and both tempo
// ends (services/game_coordinator/games/base.py), with a median of about 0.85.
// DangerElevatedRatio = 0.6 marks a player clearly working the controller but
// with a third of the headroom left.
const (
	DangerNearRatio     = 0.85
	DangerElevatedRatio = 0.6
)

// classifyDanger returns the danger category of intensity against threshold,
// "" when either is unknown or the threshold is not positive.
func classifyDanger(intensity, threshold *float64) string {
	if intensity == nil || threshold == nil || !finite(*intensity) || !finite(*threshold) || *threshold <= 0 {
		return ""
	}
	switch ratio := *intensity / *threshold; {
	case ratio >= DangerNearRatio:
		return DangerNearThreshold
	case ratio >= DangerElevatedRatio:
		return DangerElevated
	default:
		return DangerSafe
	}
}

// applyDangerSignals fills each alive player's IntensityRank, ThresholdMargin
// and Danger, and returns the session-level most-headroom / struggling hints.
// players must be sorted by serial. Ties never produce a hint.
//
//   - IntensityRank: 1 = highest current intensity among the alive players with
//     an observed (finite) intensity; equal intensities share the better rank.
//   - ThresholdMargin: death threshold - intensity (g-force), rounded to 0.01;
//     negative when over it. nil when either is unknown.
//   - mostHeadroom: the alive player with the strictly lowest current
//     intensity (the calmest right now, the most headroom below the
//     threshold), with at least two alive players observed. It is a momentary
//     reading, NOT "winning": a calm player is not a target for punishment.
//   - struggling: the alive player with the strictly highest intensity, when
//     at least two alive players are observed and that player's danger is
//     elevated or near_threshold (needs a known threshold).
func applyDangerSignals(players []PlayerState, threshold *float64) (mostHeadroom, struggling string) {
	var observed []int
	for i, p := range players {
		if p.Alive && p.Intensity != nil && finite(*p.Intensity) {
			observed = append(observed, i)
		}
	}
	for _, i := range observed {
		p := &players[i]
		higher := 0
		for _, j := range observed {
			if *players[j].Intensity > *p.Intensity {
				higher++
			}
		}
		p.IntensityRank = higher + 1
		p.Danger = classifyDanger(p.Intensity, threshold)
		if p.Danger != "" {
			m := math.Round((*threshold-*p.Intensity)*100) / 100
			p.ThresholdMargin = &m
		}
	}
	if len(observed) < 2 {
		return "", ""
	}
	minI, maxI := observed[0], observed[0]
	minTie, maxTie := false, false
	for _, i := range observed[1:] {
		v := *players[i].Intensity
		switch {
		case v < *players[minI].Intensity:
			minI, minTie = i, false
		case v == *players[minI].Intensity:
			minTie = true
		}
		switch {
		case v > *players[maxI].Intensity:
			maxI, maxTie = i, false
		case v == *players[maxI].Intensity:
			maxTie = true
		}
	}
	if !minTie {
		mostHeadroom = players[minI].Serial
	}
	if d := players[maxI].Danger; !maxTie && (d == DangerElevated || d == DangerNearThreshold) {
		struggling = players[maxI].Serial
	}
	return mostHeadroom, struggling
}

// dangerNarrative is the danger suffix of a player's narrative line, e.g.
// " danger=near_threshold margin=0.10 intensity_rank=1/3". Empty for a player
// without signals.
func dangerNarrative(p PlayerState, ranked int) string {
	var s string
	if p.Danger != "" {
		s += " danger=" + p.Danger
	}
	if p.ThresholdMargin != nil {
		s += " margin=" + strconv.FormatFloat(*p.ThresholdMargin, 'f', 2, 64)
	}
	if p.IntensityRank > 0 {
		s += " intensity_rank=" + strconv.Itoa(p.IntensityRank) + "/" + strconv.Itoa(ranked)
	}
	if p.Shielded {
		s += " shielded"
	}
	if p.ShieldCharged {
		s += " shield_charged"
	}
	return s
}

// ShieldWindow is how long after a dispatched shield a player counts as
// Shielded: the longest shield variant (grant_shield / toughness_boost max,
// decide.MagnitudeBounds).
const ShieldWindow = 10 * time.Second

// recentlyShielded returns the serials a grant_shield / toughness_boost was
// dispatched to (not blocked, not already in place: UC-040 A35) within
// ShieldWindow before c.UpdatedAt. A
// grant_shield to a charged player does not count: in trigger mode it gave a
// charge, which is no protection until the player activates it (#1870). A
// toughness_boost is protection in either mode.
func recentlyShielded(c gamecontext.GameContext, charged map[string]bool) map[string]bool {
	out := map[string]bool{}
	for _, e := range c.Timeline {
		if e.Kind != gamecontext.EventIntervention || e.Blocked || e.InPlace || e.Serial == "" ||
			(e.Detail != "grant_shield" && e.Detail != "toughness_boost") {
			continue
		}
		if e.Detail == "grant_shield" && charged[e.Serial] {
			continue
		}
		if age := c.UpdatedAt.Sub(e.At); age >= 0 && age <= ShieldWindow {
			out[e.Serial] = true
		}
	}
	return out
}

// RankedCount is the number of alive players with an IntensityRank (the
// denominator of every rank).
func (s State) RankedCount() int {
	n := 0
	for _, p := range s.Players {
		if p.IntensityRank > 0 {
			n++
		}
	}
	return n
}
