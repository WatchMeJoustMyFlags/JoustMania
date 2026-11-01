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
func recentlyShielded(c gamecontext.GameContext, charged map[string]bool, hold bool) map[string]bool {
	out := map[string]bool{}
	for _, e := range c.Timeline {
		if e.Kind != gamecontext.EventIntervention || e.Blocked || e.InPlace || e.Serial == "" ||
			(e.Detail != "grant_shield" && e.Detail != "toughness_boost") {
			continue
		}
		// In trigger mode a charge is not protection; in a hold game a grant is
		// energy, and the protection is shield_active (#2096).
		if e.Detail == "grant_shield" && (hold || charged[e.Serial]) {
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

// Sustained-danger window limits (UC-040 A43, BR-032, #2461).
const (
	// DangerSampleStaleAfter is the oldest the newest movement reading may be,
	// against the context's UpdatedAt, for the window to count.
	DangerSampleStaleAfter = 3 * time.Second
	// DangerMaxSampleGap is the longest gap between two readings of one window:
	// the track holds about one reading per second, so a longer gap means
	// readings are missing and the window is not consecutive.
	DangerMaxSampleGap = 2500 * time.Millisecond
)

// FreshReadings reports whether a player's movement track is live at now (the
// context's UpdatedAt): the newest reading is finite, not after now, and its value
// was last new at most DangerSampleStaleAfter ago. The coordinator's gauge is
// re-exported with its last value, so a frozen reading keeps arriving with a fresh
// time: the reading is as old as the first sample of the newest run of identical
// values (when the value was last new). R4d (SustainedDanger) and the rules that
// read a player's skill level or movement variance (R3, R4, R8; UC-040 A46,
// BR-037, #2511) share this signal. Fail closed (false): no readings, a newest
// reading after now (so a zero now against readings with a real time), a frozen
// or old value, a non-finite value. A zero now against readings stamped at the
// zero time passes (now - At = 0); production contexts never carry that pair, the
// store stamps both from one clock read.
func FreshReadings(samples []gamecontext.MovementSample, now time.Time) bool {
	if len(samples) == 0 {
		return false
	}
	newest := samples[len(samples)-1]
	if !finite(newest.Intensity) {
		return false
	}
	lastNew := newest.At
	for i := len(samples) - 2; i >= 0 && samples[i].Intensity == newest.Intensity; i-- {
		lastNew = samples[i].At
	}
	return now.Sub(newest.At) >= 0 && now.Sub(lastNew) <= DangerSampleStaleAfter
}

// SustainedDanger reports whether a player's newest n movement readings are each
// at or above level x the death threshold in force at that reading (UC-040 A43,
// BR-032): a sustained near-threshold signal, never a single spike. samples is the
// player's oldest-first track (gamecontext.PlayerSignals.History), track the
// session's death-threshold track and threshold the last observed scalar (the
// fallback when the track has no earlier value). now is the context's UpdatedAt.
//
// Fail closed (false): n < 1, a non-finite or non-positive level, fewer than n
// readings, a newest reading after now, a newest value that was last new more than
// DangerSampleStaleAfter ago (a frozen gauge), a gap longer than
// DangerMaxSampleGap, a window longer than n x DangerMaxSampleGap in total, a
// reading below the level, or a reading or
// threshold that is unknown, non-finite or not positive. A reading equal to the
// one before it is the same coordinator sample seen in two buckets and counts
// once. minRatio is the lowest reading / threshold ratio of the window.
func SustainedDanger(samples []gamecontext.MovementSample, track []gamecontext.SpeedSample, threshold *float64, now time.Time, level float64, n int) (minRatio float64, ok bool) {
	if n < 1 || !finite(level) || level <= 0 || len(samples) == 0 {
		return 0, false
	}
	if !FreshReadings(samples, now) {
		return 0, false
	}
	newest := samples[len(samples)-1]
	maxSpan := time.Duration(n) * DangerMaxSampleGap
	count := 0
	var prev *gamecontext.MovementSample
	for i := len(samples) - 1; i >= 0; i-- {
		s := samples[i]
		if prev != nil && prev.At.Sub(s.At) > DangerMaxSampleGap {
			return 0, false
		}
		if newest.At.Sub(s.At) > maxSpan {
			return 0, false
		}
		thr := gamecontext.ThresholdAt(track, threshold, s.At)
		if thr == nil || !finite(*thr) || *thr <= 0 || !finite(s.Intensity) {
			return 0, false
		}
		ratio := s.Intensity / *thr
		if ratio < level {
			return 0, false
		}
		if prev == nil || ratio < minRatio {
			minRatio = ratio
		}
		if prev == nil || s.Intensity != prev.Intensity {
			count++
		}
		if count >= n {
			return minRatio, true
		}
		prev = &samples[i]
	}
	return 0, false
}
