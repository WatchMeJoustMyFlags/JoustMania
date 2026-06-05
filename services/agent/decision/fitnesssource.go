package decision

import "sync"

// LiveFitness is a goroutine-safe FitnessSource whose value the decision loop
// refreshes every cycle from the evaluated fitness.* flags (#731). The rules
// engine reads it inside Evaluate, so changing a fitness threshold takes effect
// on the next decision with no restart. Until the loop publishes the first
// value, it serves the flagd-schema defaults so the engine is never thresholdless.
type LiveFitness struct {
	mu        sync.RWMutex
	threshold FitnessThresholds
}

// NewLiveFitness builds a source seeded with the flagd-schema default
// thresholds (so the engine has sane values before the first flag publication).
func NewLiveFitness() *LiveFitness {
	return &LiveFitness{threshold: DefaultStaticConfig().FitnessThresholds}
}

// Set replaces the published fitness thresholds.
func (f *LiveFitness) Set(t FitnessThresholds) {
	f.mu.Lock()
	f.threshold = t
	f.mu.Unlock()
}

// Fitness implements FitnessSource, returning the current thresholds.
func (f *LiveFitness) Fitness() FitnessThresholds {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.threshold
}

// SetFitness lets the decision loop publish the per-cycle fitness thresholds
// into the engine (fitnessPublisher seam, mirroring SetObjectives). It is a
// no-op unless the engine was built over a *LiveFitness source
// (NewObjectiveRulesLive); engines constructed from a StaticConfig ignore the
// published value and keep their fixed thresholds.
func (r *ObjectiveRules) SetFitness(t FitnessThresholds) {
	if live, ok := r.fitness.(*LiveFitness); ok {
		live.Set(t)
	}
}
