package decision

import (
	"math"
	"time"
)

// Config source interfaces for the rules engine. These are the seam where
// issue #727 plugs in OpenFeature/flagd-backed implementations; until then the
// engine runs on StaticConfig defaults that mirror services/flagd/agent.json.
// Sources are re-read on every evaluation so a live-flag implementation takes
// effect without restarting the engine.

// ObjectivesSource provides the objective weights being pursued
// (agent `objectives` flag). Keys: endurance, balanced, accelerate, chaos.
// An empty or nil map means "no value provided" — the engine falls back to
// DefaultObjectiveWeights.
type ObjectivesSource interface {
	Objectives() map[string]float64
}

// PolicySource provides the policy constraints (agent `policy.*` flags).
type PolicySource interface {
	Policy() Policy
}

// FitnessSource provides the fitness thresholds (agent `fitness.*` flags).
type FitnessSource interface {
	Fitness() FitnessThresholds
}

// Policy holds the policy constraints the engine enforces.
type Policy struct {
	// BatteryThreshold (pct, 0-100): players below it are protected from
	// battery-draining or difficulty-raising interventions
	// (policy.battery_threshold, default 20).
	BatteryThreshold float64
	// MovementVarianceWindow gates variance-triggered rules and sets the
	// cooldown after difficulty interventions
	// (fixed 10s; the unused policy.movement_variance_window flag was removed in #1322).
	MovementVarianceWindow time.Duration
	// MaxInterventionsPerMinute is the weighted intervention budget per
	// sliding 60s window (policy.max_interventions_per_minute, default 2).
	MaxInterventionsPerMinute float64
}

// FitnessThresholds holds the per-objective fitness targets the rules
// evaluate against.
type FitnessThresholds struct {
	// EnduranceMinSessionSeconds: sessions ending earlier fail endurance
	// (fitness.endurance.min_session_seconds, default 120).
	EnduranceMinSessionSeconds float64
	// BalancedMaxSkillGap: larger skill spreads fail balance
	// (fitness.balanced.max_skill_gap, default 0.4).
	BalancedMaxSkillGap float64
	// BalancedSpikeSurvivalThreshold (fitness.balanced.spike_survival_threshold,
	// default 0.8). Reserved for #731; not used by the scaffold rules.
	BalancedSpikeSurvivalThreshold float64
	// AccelerateTargetSessionSeconds: sessions running longer trigger
	// accelerate rules (fitness.accelerate.target_session_seconds, default 60).
	AccelerateTargetSessionSeconds float64
	// ShieldDangerRatio and ShieldDangerSamples are R4's danger-shield thresholds
	// (policy.shield_danger_ratio / policy.shield_danger_samples, UC-040 A43,
	// BR-032). They are policy flags, carried here because this is the engine's
	// live per-cycle source; shieldDangerParams clamps them at use time.
	ShieldDangerRatio   float64
	ShieldDangerSamples int
	// ShieldDangerOff (policy.shield_danger_enabled = off) disables the danger path;
	// the zero value keeps it on.
	ShieldDangerOff bool
}

// Danger-shield bounds and defaults (UC-040 A43, BR-032; flags.md
// policy.shield_danger_*). The bounds keep a single reading from ever qualifying.
const (
	DefaultShieldDangerRatio   = 0.85
	DefaultShieldDangerSamples = 3
	minShieldDangerRatio       = 0.7
	maxShieldDangerRatio       = 1.0
	minShieldDangerSamples     = 2
	maxShieldDangerSamples     = 10
)

// shieldDangerParams returns the clamped danger-shield level and sample count: a
// non-positive or non-finite value gives the default, the rest is clamped to the
// bounds (BR-032).
func shieldDangerParams(fit FitnessThresholds) (level float64, samples int) {
	level = fit.ShieldDangerRatio
	if math.IsNaN(level) || math.IsInf(level, 0) || level <= 0 {
		level = DefaultShieldDangerRatio
	}
	level = min(max(level, minShieldDangerRatio), maxShieldDangerRatio)
	samples = fit.ShieldDangerSamples
	if samples <= 0 {
		samples = DefaultShieldDangerSamples
	}
	samples = min(max(samples, minShieldDangerSamples), maxShieldDangerSamples)
	return level, samples
}

// Objective names (keys of the objectives weight map).
const (
	ObjectiveEndurance  = "endurance"
	ObjectiveBalanced   = "balanced"
	ObjectiveAccelerate = "accelerate"
	ObjectiveChaos      = "chaos"
)

// experimentableObjectives is the set of objectives that have a fitness function
// (EvaluateFitness can score them). chaos is deliberately absent: it is
// unpredictability by definition and has no success/degradation target to
// measure (see fitness.go header), so an experiment declared on it could only
// ever fold the worst-case 0 and run to inconclusive. Declare uses
// IsExperimentable to fail-fast such an experiment rather than waste its budget.
var experimentableObjectives = map[string]struct{}{
	ObjectiveEndurance:  {},
	ObjectiveBalanced:   {},
	ObjectiveAccelerate: {},
}

// IsExperimentable reports whether an objective has a fitness function and can
// therefore yield a non-trivial fitness sample for an experiment. The empty
// objective is treated as experimentable: defaultGameFitness folds an empty
// objective as balanced, which IS experimentable. chaos is the sole non-
// experimentable objective today.
func IsExperimentable(objective string) bool {
	if objective == "" {
		return true
	}
	_, ok := experimentableObjectives[objective]
	return ok
}

// DefaultObjectiveWeights is the engine fallback when no objectives source is
// wired or the source provides no value (#726 acceptance criterion). The flagd
// flag's own default (balanced_focused) surfaces once #727 wires the flag.
func DefaultObjectiveWeights() map[string]float64 {
	return map[string]float64{ObjectiveEndurance: 1.0}
}

// StaticConfig implements all three sources with fixed values.
type StaticConfig struct {
	ObjectiveWeights  map[string]float64
	PolicyValues      Policy
	FitnessThresholds FitnessThresholds
}

// Objectives implements ObjectivesSource.
func (c StaticConfig) Objectives() map[string]float64 { return c.ObjectiveWeights }

// Policy implements PolicySource.
func (c StaticConfig) Policy() Policy { return c.PolicyValues }

// Fitness implements FitnessSource.
func (c StaticConfig) Fitness() FitnessThresholds { return c.FitnessThresholds }

// DefaultStaticConfig returns the defaults from the flagd agent schema
// (services/flagd/agent.json defaultVariants), with the engine's no-flag
// objective fallback.
func DefaultStaticConfig() StaticConfig {
	return StaticConfig{
		ObjectiveWeights: DefaultObjectiveWeights(),
		PolicyValues: Policy{
			BatteryThreshold:          20,
			MovementVarianceWindow:    10 * time.Second,
			MaxInterventionsPerMinute: 2,
		},
		FitnessThresholds: FitnessThresholds{
			EnduranceMinSessionSeconds:     120,
			BalancedMaxSkillGap:            0.4,
			BalancedSpikeSurvivalThreshold: 0.8,
			AccelerateTargetSessionSeconds: 60,
			ShieldDangerRatio:              DefaultShieldDangerRatio,
			ShieldDangerSamples:            DefaultShieldDangerSamples,
		},
	}
}
