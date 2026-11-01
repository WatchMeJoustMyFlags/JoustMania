package experiment

import "math"

// calibration.go: arm calibration (UC-043 A21, BR-010, #2479, #2498). The
// prediction Book hands every shadow game's tally of ACTED decision predictions
// that scored hit or miss to the registry at game end; the registry adds it to an
// in-memory, per-experiment, per-arm report that experiment.progress shows on its
// experiment.arm events. Report-only: the tally is never journaled and never
// reaches the CohortVerdict, the conclusion or the promotion. It measures the mock
// archetypes (shadow calibration), not humans.

// Span event attributes of the arm calibration on experiment.arm.
const (
	AttrExperimentArmPredictionsScored = "experiment.arm_stats.predictions_scored"
	AttrExperimentArmPredictionHits    = "experiment.arm_stats.prediction_hits"
	AttrExperimentArmPredictionBrier   = "experiment.arm_stats.prediction_brier"
)

// PredictionTally is a count of scored predictions: Scored = hits + misses,
// BrierSum the sum of (p - y)^2 over them.
type PredictionTally struct {
	Scored   int
	Hits     int
	BrierSum float64
}

// Brier is the mean Brier score of t (BrierSum / Scored), 0 when nothing is scored.
func (t PredictionTally) Brier() float64 {
	if t.Scored == 0 {
		return 0
	}
	return t.BrierSum / float64(t.Scored)
}

// valid reports whether t is a consistent, non-empty tally.
func (t PredictionTally) valid() bool {
	return t.Scored > 0 && t.Hits >= 0 && t.Hits <= t.Scored &&
		!math.IsNaN(t.BrierSum) && t.BrierSum >= 0 && t.BrierSum <= float64(t.Scored)
}

// NotePredictions records one game's prediction tally for the game while it is
// bound. The Book calls it from the game-end chain before the experiment hook,
// so the game is still in flight. The tally joins the game's arm only when
// ConcludeGame folds the game's fitness (foldCalibrationLocked); a game released
// without a fold (errored, preempted, a stale end) or torn down adds nothing. A
// game that is not one of ours (a real game, a released or unknown one) and an
// empty or inconsistent tally are ignored. It emits nothing itself: the game's
// game_concluded snapshot carries the new tally.
func (r *Registry) NotePredictions(gameID string, scored, hits int, brierSum float64) {
	t := PredictionTally{Scored: scored, Hits: hits, BrierSum: brierSum}
	if r == nil || !t.valid() {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	_, _, e := r.findGameLocked(gameID)
	if e == nil {
		return
	}
	if e.pendingCalibration == nil {
		e.pendingCalibration = map[string]PredictionTally{}
	}
	e.pendingCalibration[gameID] = e.pendingCalibration[gameID].add(t)
}

// foldCalibrationLocked moves the pending tally of gameID into arm. ConcludeGame
// calls it at the fitness fold. Assumes r.mu is held.
func (e *entry) foldCalibrationLocked(gameID, arm string) {
	t, ok := e.pendingCalibration[gameID]
	if !ok {
		return
	}
	delete(e.pendingCalibration, gameID)
	if e.calibration == nil {
		e.calibration = map[string]PredictionTally{}
	}
	e.calibration[arm] = e.calibration[arm].add(t)
}

func (t PredictionTally) add(o PredictionTally) PredictionTally {
	return PredictionTally{Scored: t.Scored + o.Scored, Hits: t.Hits + o.Hits, BrierSum: t.BrierSum + o.BrierSum}
}

// ArmCalibration returns a copy of the arm tally of experiment id (tests,
// diagnostics); nil when unknown or empty.
func (r *Registry) ArmCalibration(id string) map[string]PredictionTally {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil || len(e.calibration) == 0 {
		return nil
	}
	out := make(map[string]PredictionTally, len(e.calibration))
	for k, v := range e.calibration {
		out[k] = v
	}
	return out
}
