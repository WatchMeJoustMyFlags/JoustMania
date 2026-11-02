package calibration

import (
	"sort"

	"github.com/joustmania/agent/gamejournal"
)

const (
	// HoldoutEvery: every HoldoutEvery-th eligible game is in the holdout (BR-004).
	HoldoutEvery = 4
	// BandCount is the number of elimination-pace bands (BR-005).
	BandCount = 3
	// MinBandGames is the smallest band a fit may use (BR-005).
	MinBandGames = 8
	// MinHoldoutGames is the smallest holdout (BR-004, A22's default --min-n).
	MinHoldoutGames = 8
	// MinEligibleGames is the smallest selection a calibration runs on (BR-004,
	// A2): BandCount*MinBandGames fit games plus MinHoldoutGames holdout games.
	MinEligibleGames = BandCount*MinBandGames + MinHoldoutGames
)

// SplitHoldout splits the selected games, in journal append order, into the fit
// set and the holdout: the 4th, 8th, ... game is held out (BR-004).
func SplitHoldout(games []gamejournal.Record) (fit, holdout []gamejournal.Record) {
	for i, g := range games {
		if i%HoldoutEvery == HoldoutEvery-1 {
			holdout = append(holdout, g)
		} else {
			fit = append(fit, g)
		}
	}
	return fit, holdout
}

// Band is an elimination-pace band (BR-005).
type Band int

const (
	BandLow Band = iota
	BandMiddle
	BandHigh
)

func (b Band) String() string {
	switch b {
	case BandLow:
		return "low"
	case BandHigh:
		return "high"
	default:
		return "middle"
	}
}

// BandFor is the fixed band of an archetype name (BR-005), so its meaning does
// not swap between calibrations. Any name not listed is middle.
func BandFor(name string) Band {
	switch name {
	case "calm_long", "slow_cascade":
		return BandLow
	case "close_race", "quick_cascade":
		return BandHigh
	default:
		return BandMiddle
	}
}

// Bands splits the fit games into BandCount bands by elimination pace (low,
// middle, high), each a third, any remainder in the middle band. Ties keep the
// journal append order of the input (a stable sort).
func Bands(fit []gamejournal.Record) [BandCount][]gamejournal.Record {
	sorted := append([]gamejournal.Record(nil), fit...)
	sort.SliceStable(sorted, func(i, j int) bool {
		return eliminationPace(sorted[i]) < eliminationPace(sorted[j])
	})
	third := len(sorted) / BandCount
	var out [BandCount][]gamejournal.Record
	out[BandLow] = sorted[:third]
	out[BandMiddle] = sorted[third : len(sorted)-third]
	out[BandHigh] = sorted[len(sorted)-third:]
	return out
}

// eliminationPace is the journal's elimination_pace (eliminations per game
// minute). Eligible guarantees it is finite (BR-002 metrics_missing).
func eliminationPace(rec gamejournal.Record) float64 {
	return *rec.Metrics.EliminationPace
}
