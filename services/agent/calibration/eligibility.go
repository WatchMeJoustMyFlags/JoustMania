// Package calibration is the agent's shadow-player calibration (#1055, UC-047).
//
// This file and its siblings hold the pure selection core: which journaled real
// games are eligible (BR-002, BR-003), which of them a calibration uses (step 2,
// A1, the watermark of BR-007), how they split into fit set and holdout (BR-004)
// and into elimination-pace bands (BR-005). No I/O, no clock, no flag reads:
// the caller passes the journal's records, oldest first.
package calibration

import (
	"math"
	"sort"

	"github.com/joustmania/agent/gamejournal"
)

// Constants of UC-047 BR-008 (reviewed edits, not flags, C-035).
const (
	// FFAMode is the only mode pattern games run (C-021).
	FFAMode = "JoustFFA"
	// MinDurationSeconds is the shortest eligible real game.
	MinDurationSeconds = 10.0
	// MovementFloor is the per-player mean movement variance (g²) at or above
	// which a player moved.
	MovementFloor = 0.005
	// DominanceRatio: the highest variance must be below this times the second.
	DominanceRatio = 10.0
	// MinMovedPlayers is the number of players that must have moved.
	MinMovedPlayers = 2
)

// Reason names why a journaled game is not eligible (BR-002, BR-003). The
// checks run in the order of the constants; the first failing one counts.
type Reason string

const (
	ReasonOtherMode      Reason = "other_mode"
	ReasonTestSession    Reason = "test_session"
	ReasonMetricsMissing Reason = "metrics_missing"
	ReasonTooShort       Reason = "too_short"
	ReasonTooFewMoved    Reason = "too_few_moved"
	ReasonDominant       Reason = "dominant"
)

// Eligible reports whether one journaled real game may be fitted or held out
// (BR-002). It returns ("", true) for an eligible game, else the first failing
// reason and false. The watermark (BR-007) is not checked here: see Select.
func Eligible(rec gamejournal.Record) (Reason, bool) {
	if rec.Mode != FFAMode {
		return ReasonOtherMode, false
	}
	// A null mark means the flag was unreadable at the game end: never treat
	// such a game as non-test (BR-003).
	if rec.TestSession == nil || *rec.TestSession {
		return ReasonTestSession, false
	}
	if !finite(rec.DurationSeconds) {
		return ReasonMetricsMissing, false
	}
	variances := make([]float64, 0, len(rec.Players))
	for _, p := range rec.Players {
		if !finite(p.MeanMovementVariance) {
			return ReasonMetricsMissing, false
		}
		variances = append(variances, *p.MeanMovementVariance)
	}
	if *rec.DurationSeconds < MinDurationSeconds {
		return ReasonTooShort, false
	}
	moved := 0
	for _, v := range variances {
		if v >= MovementFloor {
			moved++
		}
	}
	if moved < MinMovedPlayers {
		return ReasonTooFewMoved, false
	}
	sort.Sort(sort.Reverse(sort.Float64Slice(variances)))
	if !(variances[0] < DominanceRatio*variances[1]) {
		return ReasonDominant, false
	}
	return "", true
}

func finite(v *float64) bool {
	return v != nil && !math.IsNaN(*v) && !math.IsInf(*v, 0)
}
