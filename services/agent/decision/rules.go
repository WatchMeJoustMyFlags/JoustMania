package decision

import (
	"context"
	"math/rand/v2"
	"sort"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// Engine tuning constants.
const (
	// evalInterval bounds how often the rule set actually runs: Evaluate is
	// invoked ~10x/s from the Export handlers during games, but rules only
	// need ~1 Hz; the weighted budget further caps emissions.
	evalInterval = time.Second
	// minScore drops candidates whose urgency x objective-weight is too weak
	// to act on (also drops everything for objectives with ~zero weight).
	minScore = 0.10
	// maxDecisionsPerEval caps emissions per evaluation so one pass cannot
	// spend the whole per-minute budget at once.
	maxDecisionsPerEval = 2
)

// ObjectiveRules is the rules engine of #726: deterministic, objective-weighted
// decision logic — the rules_decide(context, objectives) path of the decision
// loop and the final link of the inference fallback chain. Each rule yields
// candidates with an urgency and the objective they serve; candidates are
// scored urgency x weight[objective], filtered by policy (battery, variance
// cooldown), and admitted best-first while the weighted per-minute budget
// allows. Safe for concurrent use (Evaluate is called from concurrent Export
// handler goroutines).
type ObjectiveRules struct {
	objectives ObjectivesSource
	policy     PolicySource
	fitness    FitnessSource

	mu               sync.Mutex
	now              func() time.Time
	rng              *rand.Rand
	lastEval         time.Time
	lastDifficultyAt time.Time
	limiter          windowLimiter
}

// NewObjectiveRules builds the engine. cfg may be nil, in which case
// DefaultStaticConfig (flagd-schema defaults, objectives {endurance:1.0}) is
// used; #727 passes OpenFeature-backed sources instead.
func NewObjectiveRules(cfg *StaticConfig) *ObjectiveRules {
	c := DefaultStaticConfig()
	if cfg != nil {
		c = *cfg
	}
	return newObjectiveRules(c, c, c, nil, nil)
}

// newObjectiveRules is the fully-injectable constructor used by tests.
// now nil -> time.Now; rng nil -> time-seeded.
func newObjectiveRules(obj ObjectivesSource, pol PolicySource, fit FitnessSource, now func() time.Time, rng *rand.Rand) *ObjectiveRules {
	if now == nil {
		now = time.Now
	}
	if rng == nil {
		n := time.Now()
		rng = rand.New(rand.NewPCG(uint64(n.UnixNano()), uint64(n.UnixNano()>>32))) // #nosec G404 -- non-cryptographic: chaos-rule jitter only
	}
	return &ObjectiveRules{
		objectives: obj,
		policy:     pol,
		fitness:    fit,
		now:        now,
		rng:        rng,
	}
}

// Evaluate implements RulesEngine: run the rule set (at most once per
// evalInterval), score candidates by the objective weights, apply policy
// filters, and emit the best decisions the weighted budget allows.
func (r *ObjectiveRules) Evaluate(_ context.Context, c gamecontext.GameContext) []Decision {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := r.now()
	if now.Sub(r.lastEval) < evalInterval {
		return nil
	}
	r.lastEval = now

	weights := normalizedWeights(r.objectives.Objectives())
	pol := r.policy.Policy()
	fit := r.fitness.Fitness()

	// R9's randomness is drawn here so the rule functions stay pure.
	roll, rollTarget := r.chaosRoll(c)

	cands := enduranceCandidates(c, fit)
	cands = append(cands, balancedCandidates(c, fit)...)
	cands = append(cands, accelerateCandidates(c, fit, weights)...)
	cands = append(cands, chaosCandidates(c, pol, roll, rollTarget)...)

	cands = applyBatteryPolicy(cands, c, pol, weights)
	cands = r.applyVarianceCooldown(cands, now, pol)
	cands = scoreAndSort(cands, weights)

	var out []Decision
	for _, cd := range cands {
		if len(out) == maxDecisionsPerEval {
			break
		}
		if !r.limiter.admit(now, cd.cost, pol.MaxInterventionsPerMinute) {
			continue
		}
		d := cd.decision
		d.Objectives = weights
		out = append(out, d)
		if cd.difficulty {
			// Difficulty interventions invalidate the movement-variance
			// baseline: players change behavior in response (#722 §5).
			r.lastDifficultyAt = now
		}
	}
	return out
}

// chaosRoll draws R9's randomness: a roll in [0,1) and a random active player.
// Caller holds r.mu (rand.Rand is not goroutine-safe).
func (r *ObjectiveRules) chaosRoll(c gamecontext.GameContext) (float64, string) {
	active := activePlayers(c)
	if len(active) == 0 {
		return 0, ""
	}
	return r.rng.Float64(), active[r.rng.IntN(len(active))].Serial
}

// normalizedWeights returns a COPY of the active objective weights, falling
// back to DefaultObjectiveWeights when the source provides none (#726
// acceptance: works with {endurance: 1.0} when no flag value is provided).
// The copy matters: the weights are handed out on every emitted Decision and
// read later on other goroutines (span rendering); a live map from a future
// OpenFeature-backed source (#727) must not be shared unprotected.
func normalizedWeights(w map[string]float64) map[string]float64 {
	if len(w) == 0 {
		return DefaultObjectiveWeights()
	}
	cp := make(map[string]float64, len(w))
	for k, v := range w {
		cp[k] = v
	}
	return cp
}

// applyBatteryPolicy enforces policy.battery_threshold (#722 §5): players
// below the threshold are protected from the dominant battery drains
// (controller effects) and from difficulty-raising demand; eliminate_player
// stays available only as the graceful-exit path when accelerate dominates.
// Session-scoped difficulty raises are dropped when ANY active player is below
// the threshold. Kept low-battery eliminations record the battery fitness.
func applyBatteryPolicy(cands []candidate, c gamecontext.GameContext, pol Policy, weights map[string]float64) []candidate {
	if pol.BatteryThreshold <= 0 {
		return cands // policy off
	}
	low := func(p *gamecontext.PlayerSignals) bool {
		return p != nil && p.BatteryPct != nil && *p.BatteryPct < pol.BatteryThreshold
	}
	anyActiveLow := false
	for _, p := range activePlayers(c) {
		if low(p) {
			anyActiveLow = true
			break
		}
	}

	kept := cands[:0]
	for _, cd := range cands {
		target := c.Players[cd.decision.TargetSerial]
		switch {
		case cd.decision.Intervention == InterventionSendControllerEffect && low(target):
			continue // rumble/LED on a dying battery — blocked
		case cd.difficulty && cd.decision.TargetSerial != "" && low(target):
			continue // more demand on a dying battery — blocked
		case cd.difficulty && cd.decision.TargetSerial == "" && anyActiveLow:
			continue // session-wide demand raise while someone is low — blocked
		case cd.decision.Intervention == InterventionEliminatePlayer && low(target):
			if !accelerateDominant(weights) {
				continue // graceful exit is an accelerate move only
			}
			if cd.decision.Fitness == nil {
				cd.decision.Fitness = map[string]float64{}
			}
			cd.decision.Fitness["battery_pct"] = *target.BatteryPct
			cd.decision.Fitness["battery_threshold"] = pol.BatteryThreshold
		}
		kept = append(kept, cd)
	}
	return kept
}

// applyVarianceCooldown enforces policy.movement_variance_window: after any
// difficulty intervention the variance baseline is invalid and players are
// still adapting, so ALL chaos candidates — variance-triggered (R8) and the
// random nudge (R9) alike — are suppressed for one full window. Suppressing
// R9 too is deliberate: a random rumble right after a tempo/sensitivity change
// muddies attribution of the difficulty intervention's effect. Caller holds r.mu.
func (r *ObjectiveRules) applyVarianceCooldown(cands []candidate, now time.Time, pol Policy) []candidate {
	if r.lastDifficultyAt.IsZero() || now.Sub(r.lastDifficultyAt) >= pol.MovementVarianceWindow {
		return cands
	}
	kept := cands[:0]
	for _, cd := range cands {
		if cd.objective == ObjectiveChaos {
			continue
		}
		kept = append(kept, cd)
	}
	return kept
}

// scoreAndSort scores candidates (urgency x weight[objective]), drops those
// below minScore, and orders them deterministically: higher score first, then
// cheaper/safer, then lexicographic intervention and target.
func scoreAndSort(cands []candidate, weights map[string]float64) []candidate {
	type scored struct {
		candidate
		score float64
	}
	kept := make([]scored, 0, len(cands))
	for _, cd := range cands {
		s := clamp01(cd.urgency) * weights[cd.objective]
		if s < minScore {
			continue
		}
		kept = append(kept, scored{cd, s})
	}
	sort.SliceStable(kept, func(i, j int) bool {
		if kept[i].score != kept[j].score {
			return kept[i].score > kept[j].score
		}
		if kept[i].cost != kept[j].cost {
			return kept[i].cost < kept[j].cost
		}
		if kept[i].decision.Intervention != kept[j].decision.Intervention {
			return kept[i].decision.Intervention < kept[j].decision.Intervention
		}
		return kept[i].decision.TargetSerial < kept[j].decision.TargetSerial
	})
	out := make([]candidate, len(kept))
	for i, s := range kept {
		out[i] = s.candidate
	}
	return out
}
