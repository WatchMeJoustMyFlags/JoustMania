package decision

import (
	"testing"
	"time"
)

func TestDefaultStaticConfig_MatchesFlagdSchema(t *testing.T) {
	cfg := DefaultStaticConfig()

	if w := cfg.ObjectiveWeights; len(w) != 1 || w[ObjectiveEndurance] != 1.0 {
		t.Errorf("objectives = %v, want {endurance: 1.0} (engine no-flag default)", w)
	}
	pol := cfg.PolicyValues
	if pol.BatteryThreshold != 20 {
		t.Errorf("battery threshold = %v, want 20", pol.BatteryThreshold)
	}
	if pol.MovementVarianceWindow != 10*time.Second {
		t.Errorf("variance window = %v, want 10s", pol.MovementVarianceWindow)
	}
	if pol.MaxInterventionsPerMinute != 2 {
		t.Errorf("max interventions/min = %v, want 2", pol.MaxInterventionsPerMinute)
	}
	fit := cfg.FitnessThresholds
	if fit.EnduranceMinSessionSeconds != 120 || fit.BalancedMaxSkillGap != 0.4 ||
		fit.BalancedSpikeSurvivalThreshold != 0.8 || fit.AccelerateTargetSessionSeconds != 60 {
		t.Errorf("fitness thresholds = %+v, want flagd schema defaults 120/0.4/0.8/60", fit)
	}
}

func TestNewObjectiveRules_NilConfigUsesDefaults(t *testing.T) {
	r := NewObjectiveRules(nil)
	if w := r.objectives.Objectives(); len(w) != 1 || w[ObjectiveEndurance] != 1.0 {
		t.Errorf("nil config objectives = %v, want {endurance: 1.0}", w)
	}
	if r.policy.Policy().MaxInterventionsPerMinute != 2 {
		t.Error("nil config must fall back to default policy")
	}
}

func TestNormalizedWeights_EmptyFallsBack(t *testing.T) {
	if w := normalizedWeights(nil); w[ObjectiveEndurance] != 1.0 {
		t.Errorf("nil weights = %v, want endurance fallback", w)
	}
	if w := normalizedWeights(map[string]float64{}); w[ObjectiveEndurance] != 1.0 {
		t.Errorf("empty weights = %v, want endurance fallback", w)
	}
	custom := map[string]float64{ObjectiveChaos: 0.9}
	if w := normalizedWeights(custom); w[ObjectiveChaos] != 0.9 {
		t.Errorf("custom weights = %v, want passthrough", w)
	}
}
