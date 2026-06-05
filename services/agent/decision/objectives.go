package decision

import "sync"

// LiveObjectives is a goroutine-safe ObjectivesSource whose value the decision
// loop refreshes every cycle from the evaluated `objectives` flag (#727). The
// rules engine reads it inside Evaluate, so a flag change takes effect on the
// next decision with no restart. An empty/nil value means "no objectives flag
// resolved" and the engine falls back to DefaultObjectiveWeights (#726).
type LiveObjectives struct {
	mu      sync.RWMutex
	weights map[string]float64
}

// NewLiveObjectives builds an empty source (engine fallback applies until the
// loop publishes the first flag value).
func NewLiveObjectives() *LiveObjectives { return &LiveObjectives{} }

// Set replaces the published objective weights. A defensive copy is stored so
// the caller's map can be mutated freely afterwards.
func (o *LiveObjectives) Set(weights map[string]float64) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(weights) == 0 {
		o.weights = nil
		return
	}
	cp := make(map[string]float64, len(weights))
	for k, v := range weights {
		cp[k] = v
	}
	o.weights = cp
}

// Objectives implements ObjectivesSource, returning a copy of the current
// weights (or nil, which the engine reads as "use the default weighting").
func (o *LiveObjectives) Objectives() map[string]float64 {
	o.mu.RLock()
	defer o.mu.RUnlock()
	if len(o.weights) == 0 {
		return nil
	}
	cp := make(map[string]float64, len(o.weights))
	for k, v := range o.weights {
		cp[k] = v
	}
	return cp
}

// SetObjectives lets the decision loop publish the per-cycle objectives flag
// into the engine (objectivePublisher seam). It is a no-op unless the engine
// was built over a *LiveObjectives source (NewObjectiveRulesLive); engines
// constructed from a StaticConfig ignore the published value and keep their
// fixed weights.
func (r *ObjectiveRules) SetObjectives(weights map[string]float64) {
	if live, ok := r.objectives.(*LiveObjectives); ok {
		live.Set(weights)
	}
}

// NewObjectiveRulesLive builds the rules engine with a *LiveObjectives source
// so the decision loop can drive the objective weights from the `objectives`
// flag each cycle (#727). policy and fitness come from cfg (nil -> flagd-schema
// defaults). The returned engine satisfies objectivePublisher.
func NewObjectiveRulesLive(cfg *StaticConfig) *ObjectiveRules {
	c := DefaultStaticConfig()
	if cfg != nil {
		c = *cfg
	}
	return newObjectiveRules(NewLiveObjectives(), c, c, nil, nil)
}
