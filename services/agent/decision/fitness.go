package decision

import (
	"math"
	"sort"

	"github.com/joustmania/agent/gamecontext"
)

// Fitness functions (#731) score the live game context against the per-objective
// thresholds resolved from the fitness.* flags. They turn "is this session
// succeeding for objective X?" into observable, runtime-tunable numbers that
// steer action selection and ride onto the decision span as fitness.evaluated.
//
// Three objectives have a fitness function; chaos does not. chaos is
// unpredictability by definition (docs/research/722-intervention-surface.md §4:
// "chaos (unpredictability)"), so there is no success/degradation target to
// measure — a chaos objective neither passes nor fails a fitness check and so
// contributes nothing here. This is intentional, not an omission.
//
// Each function emits:
//   - threshold values (so the trace records the flag in effect), and
//   - a normalized 0..1 progress toward the objective's target, plus a
//     satisfied bool, when the signals it needs are present.
//
// Missing signals are handled gracefully: a function whose required signal is
// unknown (nil session duration, no skill levels, …) is SKIPPED — it emits
// neither a progress value nor a satisfied flag, so the engine never fabricates
// a fitness number from absent data.

// FitnessResult is one objective's fitness evaluation for a cycle.
type FitnessResult struct {
	// Objective is the objective this result scores (endurance/balanced/...).
	Objective string
	// Evaluated is true when the required signals were present and the function
	// ran. False means the function was skipped (missing signals) and Progress /
	// Satisfied are meaningless.
	Evaluated bool
	// Progress is the normalized 0..1 progress toward satisfying the objective:
	// 1.0 = fully satisfied, 0.0 = maximally failing. Only meaningful when
	// Evaluated.
	Progress float64
	// Satisfied reports whether the objective's fitness target is currently met.
	// Only meaningful when Evaluated.
	Satisfied bool
	// Pressure is 1-Progress: how strongly a failing fitness should amplify
	// candidates serving this objective. 0 when satisfied, up to 1 when maximally
	// failing. Only meaningful when Evaluated.
	Pressure float64
	// Values holds the threshold + computed signals to record on the span under
	// dotted keys (e.g. "endurance.session_progress").
	Values map[string]float64
}

// FitnessEvaluation is the full per-cycle fitness picture across objectives.
type FitnessEvaluation struct {
	// Results is keyed by objective name; only objectives whose function ran are
	// present (chaos is never present — it has no fitness function).
	Results map[string]FitnessResult
}

// Pressure returns the failing-fitness pressure for an objective: 0 when the
// objective is satisfied, was not evaluated (missing signals), or has no fitness
// function (chaos). A higher value means the objective is failing and its
// candidates should be amplified.
func (e FitnessEvaluation) Pressure(objective string) float64 {
	r, ok := e.Results[objective]
	if !ok || !r.Evaluated {
		return 0
	}
	return r.Pressure
}

// Evaluated reports the cycle-level fitness values for the span's
// fitness.evaluated attribute: every threshold and every computed signal across
// all objectives that ran, under dotted "objective.signal" keys.
func (e FitnessEvaluation) Evaluated() map[string]float64 {
	out := make(map[string]float64)
	for _, r := range e.Results {
		for k, v := range r.Values {
			out[k] = v
		}
	}
	return out
}

// EvaluateFitness runs every objective's fitness function against the live game
// context and the flag-resolved thresholds. Functions whose required signals are
// missing are skipped (not present in the result), never fabricated.
func EvaluateFitness(c gamecontext.GameContext, fit FitnessThresholds) FitnessEvaluation {
	eval := FitnessEvaluation{Results: make(map[string]FitnessResult, 3)}
	if r, ok := evaluateEndurance(c, fit); ok {
		eval.Results[ObjectiveEndurance] = r
	}
	if r, ok := evaluateBalanced(c, fit); ok {
		eval.Results[ObjectiveBalanced] = r
	}
	if r, ok := evaluateAccelerate(c, fit); ok {
		eval.Results[ObjectiveAccelerate] = r
	}
	return eval
}

// evaluateEndurance: endurance wants long sessions. Progress is the session
// duration as a fraction of the min-session target, clamped to 1.0. Satisfied
// once the session has reached the target. Requires session duration; skipped
// when it is unknown.
func evaluateEndurance(c gamecontext.GameContext, fit FitnessThresholds) (FitnessResult, bool) {
	dur := sessionDuration(c)
	if dur < 0 || fit.EnduranceMinSessionSeconds <= 0 {
		return FitnessResult{}, false
	}
	progress := clamp01(dur / fit.EnduranceMinSessionSeconds)
	return FitnessResult{
		Objective: ObjectiveEndurance,
		Evaluated: true,
		Progress:  progress,
		Satisfied: dur >= fit.EnduranceMinSessionSeconds,
		Pressure:  1 - progress,
		Values: map[string]float64{
			"endurance.min_session_seconds": fit.EnduranceMinSessionSeconds,
			"endurance.session_seconds":     dur,
			"endurance.session_progress":    progress,
		},
	}, true
}

// evaluateBalanced: balanced binds on SKILL-GAP ONLY at conclusion.
//
// Skill gap (the binding signal): the spread (max-min) of per-player skill_level.
// Satisfied when the gap is within max_skill_gap. Requires at least two players
// with a skill level; the result is skipped (not evaluated) otherwise. Balanced's
// Progress and Satisfied derive from skill-gap alone.
//
// Spike survival (#1024): OBSERVABILITY ONLY — recorded for future calibration,
// NOT gating. The conclusion-time spike metric is unresolved: the std/peak ratio
// is scale-invariant, so it is effectively constant and carries no defensible
// signal without real-game calibration (two review rounds confirmed this). The
// calibrated metric is tracked separately as #1125. We still COMPUTE the ratio
// and record balanced.spike_survival_ratio / balanced.spike_survival_progress as
// span values (the data foundation for #1125, fed by the retained whole-game
// aggregates), but the ratio does NOT contribute to balanced Progress (it does
// not bind min(progresses)) and does NOT set Satisfied=false.
//
// The balanced result is emitted whenever the skill-gap sub-check could be
// computed; Progress/Satisfied/Pressure come from skill-gap only.
func evaluateBalanced(c gamecontext.GameContext, fit FitnessThresholds) (FitnessResult, bool) {
	values := map[string]float64{
		"balanced.max_skill_gap":            fit.BalancedMaxSkillGap,
		"balanced.spike_survival_threshold": fit.BalancedSpikeSurvivalThreshold,
	}

	// Spike-survival: observability only — recorded, never gating (#1125).
	if ratio, ok := spikeSurvivalRatio(c); ok {
		values["balanced.spike_survival_ratio"] = ratio
		values["balanced.spike_survival_progress"] = survivalProgress(ratio, fit.BalancedSpikeSurvivalThreshold)
	}

	// Skill-gap is the ONLY binding sub-check.
	gap, ok := skillGap(c)
	if !ok {
		return FitnessResult{}, false
	}
	values["balanced.skill_gap"] = gap
	gapProgress := skillGapProgress(gap, fit.BalancedMaxSkillGap)
	values["balanced.skill_gap_progress"] = gapProgress

	return FitnessResult{
		Objective: ObjectiveBalanced,
		Evaluated: true,
		Progress:  gapProgress,
		Satisfied: gap <= fit.BalancedMaxSkillGap,
		Pressure:  1 - gapProgress,
		Values:    values,
	}, true
}

// evaluateAccelerate: accelerate wants short sessions, so overshooting the
// target is failing. Progress is 1.0 up to the target and decays toward 0 as the
// session runs past it (a full target's worth past = 0). Satisfied while at or
// under the target. Requires session duration; skipped when unknown.
func evaluateAccelerate(c gamecontext.GameContext, fit FitnessThresholds) (FitnessResult, bool) {
	dur := sessionDuration(c)
	if dur < 0 || fit.AccelerateTargetSessionSeconds <= 0 {
		return FitnessResult{}, false
	}
	target := fit.AccelerateTargetSessionSeconds
	overshoot := dur - target
	var progress float64
	switch {
	case overshoot <= 0:
		progress = 1
	default:
		// One full target past the goal drives progress to 0.
		progress = clamp01(1 - overshoot/target)
	}
	return FitnessResult{
		Objective: ObjectiveAccelerate,
		Evaluated: true,
		Progress:  progress,
		Satisfied: dur <= target,
		Pressure:  1 - progress,
		Values: map[string]float64{
			"accelerate.target_session_seconds": target,
			"accelerate.session_seconds":        dur,
			"accelerate.session_progress":       progress,
		},
	}, true
}

// skillGap returns the spread (max-min) of skill levels across all players that
// report one, and whether at least two such players exist.
func skillGap(c gamecontext.GameContext) (float64, bool) {
	var levels []float64
	for _, p := range c.Players {
		if p != nil && p.SkillLevel != nil {
			levels = append(levels, *p.SkillLevel)
		}
	}
	if len(levels) < 2 {
		return 0, false
	}
	minV, maxV := levels[0], levels[0]
	for _, v := range levels[1:] {
		if v < minV {
			minV = v
		}
		if v > maxV {
			maxV = v
		}
	}
	return maxV - minV, true
}

// skillGapProgress maps a skill gap to 0..1 progress: 1.0 at gap 0, decaying to
// 0 as the gap reaches double the allowed maximum (so being exactly at the
// threshold is the 0.5 boundary). A non-positive threshold means "any gap is
// failing", returning 0.
func skillGapProgress(gap, maxGap float64) float64 {
	if maxGap <= 0 {
		return 0
	}
	return clamp01(1 - gap/(2*maxGap))
}

// spikeSurvivalRatio derives the fraction of players who survive movement spikes
// over the WHOLE game from each player's RETAINED whole-game aggregates —
// MovementVarianceAggregate (cumulative Welford variance over every frame) and
// PeakAccel (the whole-game peak accel magnitude). Both inputs are retained into
// the conclusion snapshot (like SkillLevel), so the ratio is computable at game
// conclusion; it iterates every player (c.Players directly, like skillGap) and is
// not Active-gated.
//
// OBSERVABILITY ONLY (#1125): this ratio is recorded as a span value but does NOT
// gate balanced fitness. The conclusion-time spike metric is unresolved — the
// std/peak ratio is scale-invariant, so it is effectively constant and carries no
// defensible signal without real-game calibration (two review rounds confirmed
// this). spikeSurvivalMaxStdPeakRatio (0.30) is kept as the survivor bound so the
// recorded ratio stays comparable across games while calibration (#1125) is
// pending; it is not load-bearing for any decision today.
const spikeSurvivalMaxStdPeakRatio = 0.30

// A player "survives" when their whole-game movement std-deviation is a small
// fraction of their whole-game peak intensity (std/peak <= spikeSurvivalMaxStdPeakRatio):
// std = sqrt(MovementVarianceAggregate); both std and PeakAccel are in g. Returns
// the ratio and whether any player had both retained aggregates. Observability
// only — see the const comment and #1125; the ratio is recorded, never gating.
func spikeSurvivalRatio(c gamecontext.GameContext) (float64, bool) {
	var total, survivors int
	for _, p := range c.Players {
		if p == nil || p.MovementVarianceAggregate == nil || p.PeakAccel == nil {
			continue
		}
		peak := *p.PeakAccel
		if peak <= 0 {
			// No peak observed → no spike to survive; treat as a survivor so a
			// motionless/never-moved player isn't counted as spike-thrown.
			total++
			survivors++
			continue
		}
		total++
		std := math.Sqrt(*p.MovementVarianceAggregate)
		if std <= spikeSurvivalMaxStdPeakRatio*peak {
			survivors++
		}
	}
	if total == 0 {
		return 0, false
	}
	return float64(survivors) / float64(total), true
}

// survivalProgress maps a survival ratio to 0..1 progress relative to its
// threshold: the ratio scaled so that hitting the threshold yields 1.0 (fully
// satisfied) and 0 survival yields 0. A non-positive threshold means any
// survival passes, returning 1.0 whenever the check ran.
func survivalProgress(ratio, threshold float64) float64 {
	if threshold <= 0 {
		return 1
	}
	return clamp01(ratio / threshold)
}

// sortedFitnessKeys returns the Values keys of an evaluation sorted, used by
// tests to assert a stable key vocabulary.
func sortedFitnessKeys(e FitnessEvaluation) []string {
	seen := e.Evaluated()
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
