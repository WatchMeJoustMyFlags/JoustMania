package decision

import (
	"context"
	"math/rand/v2"
	"sort"
	"sync"
	"time"

	"github.com/joustmania/agent/decide"
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
	// lastFitness is the most recent per-cycle fitness evaluation, retained so
	// the decision loop can lift its cycle-level values onto the span as
	// fitness.evaluated (#731).
	lastFitness FitnessEvaluation

	// UC-046 BR-007: in a pattern game the chaos RNG is a per-game PCG seeded from
	// the game's rng_seed. seedSource is consulted until it answers (the game may be
	// registered as a pattern game after its loop was built); nil = never seeded,
	// the time-seeded RNG stays, and the rules path is byte-unchanged.
	seedSource func() (uint64, bool)
	seeded     bool
	chaosDraws uint64
	// lateSeed is called when the engine is seeded after it already drew chaos
	// rolls from the time-seeded RNG (the game is then non-exact, BR-007).
	lateSeed func()

	// shieldSource is the loop's shield-ledger view (UC-040 A31, A33, #1870): read
	// once per ranking, after the throttle. charged feeds the state the ranking
	// builds, noShield the no-shield filter. nil (the golden harness, every older
	// test) means no player is charged or limited.
	shieldSource func() (charged, noShield map[string]bool)

	// appliedSource is the loop's applied-value view (UC-040 A35, Loop.AppliedValues):
	// read once per ranking. nil (the golden harness, every older test) filters
	// nothing.
	appliedSource func() map[string]string
}

// SetAppliedSource installs the applied-value view (Loop.AppliedValues). It is called
// under the engine lock, so it must not call back into the engine.
func (r *ObjectiveRules) SetAppliedSource(src func() map[string]string) {
	r.mu.Lock()
	r.appliedSource = src
	r.mu.Unlock()
}

// SetChargedSource installs a charge-only ledger view: the charged set is also the
// no-shield set. It is called under the engine lock, so it must not call back into
// the engine.
func (r *ObjectiveRules) SetChargedSource(src func() map[string]bool) {
	if src == nil {
		r.SetShieldSource(nil)
		return
	}
	r.SetShieldSource(func() (map[string]bool, map[string]bool) {
		charged := src()
		return charged, charged
	})
}

// SetShieldSource installs the shield-ledger view (Loop.ShieldSerials). It is called
// under the engine lock, so it must not call back into the engine.
func (r *ObjectiveRules) SetShieldSource(src func() (charged, noShield map[string]bool)) {
	r.mu.Lock()
	r.shieldSource = src
	r.mu.Unlock()
}

// shieldLocked takes the one ledger snapshot of a ranking. Caller holds r.mu.
func (r *ObjectiveRules) shieldLocked() (charged, noShield map[string]bool) {
	if r.shieldSource == nil {
		return nil, nil
	}
	return r.shieldSource()
}

// SetLateSeedHook installs the callback for a seed that arrived after chaos draws.
func (r *ObjectiveRules) SetLateSeedHook(fn func()) {
	r.mu.Lock()
	r.lateSeed = fn
	r.mu.Unlock()
}

// SetChaosSeedSource installs the per-game chaos seed lookup (UC-046 BR-007).
func (r *ObjectiveRules) SetChaosSeedSource(src func() (uint64, bool)) {
	r.mu.Lock()
	r.seedSource = src
	r.mu.Unlock()
}

// ChaosDraws is the number of chaos draws this engine made (decision.chaos_draw_index).
func (r *ObjectiveRules) ChaosDraws() uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.chaosDraws
}

// maybeSeedLocked switches to the per-game seeded RNG once the seed is known.
// Caller holds r.mu.
func (r *ObjectiveRules) maybeSeedLocked() {
	if r.seeded || r.seedSource == nil {
		return
	}
	if seed, ok := r.seedSource(); ok {
		// UC-046 BR-007: per-game PCG, so the chaos draws replay with the game.
		r.rng = rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15)) // #nosec G404 -- non-cryptographic chaos jitter
		r.seeded = true
		if r.chaosDraws > 0 && r.lateSeed != nil {
			r.lateSeed()
		}
	}
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

// LastFitness returns the most recent per-cycle fitness evaluation. The decision
// loop reads it after Evaluate to lift the cycle-level fitness.evaluated values
// onto the decision span (#731). Safe for concurrent use.
func (r *ObjectiveRules) LastFitness() FitnessEvaluation {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.lastFitness
}

// Evaluate implements RulesEngine: run the rule set (at most once per
// evalInterval), score candidates by the objective weights, apply policy
// filters, and emit the best decisions the weighted budget allows.
func (r *ObjectiveRules) Evaluate(_ context.Context, c gamecontext.GameContext) []Decision {
	out, _, _, _ := r.evaluate(c, nil)
	return out
}

// EvaluateDecide is Evaluate plus the decide-contract answers (#1226), both
// derived from ONE ranking: one rankLocked call and therefore one chaos (R9) RNG
// draw per evaluation, exactly as Evaluate alone. The returned decisions are
// byte-for-byte what Evaluate would have returned for the same engine state.
//
// build constructs the decide.Request lazily: it is called (under the engine
// lock, so it must be pure and cheap enough) only when the evalInterval throttle
// lets this evaluation actually rank, so the per-Export hot path never builds a
// State it would throw away. It receives the ranking's shield-charge snapshot
// (#1870), so the state marks exactly the players the ranking skipped. ranked is false on a throttled call (no decisions,
// no answers, req and resp are zero).
func (r *ObjectiveRules) EvaluateDecide(_ context.Context, c gamecontext.GameContext, build func(charged map[string]bool) decide.Request) (out []Decision, req decide.Request, resp decide.Response, ranked bool) {
	return r.evaluate(c, build)
}

// evaluate is the shared body of Evaluate and EvaluateDecide. With a nil build it
// is exactly the pre-#1226 Evaluate.
func (r *ObjectiveRules) evaluate(c gamecontext.GameContext, build func(charged map[string]bool) decide.Request) ([]Decision, decide.Request, decide.Response, bool) {
	r.mu.Lock()
	defer r.mu.Unlock()

	now := r.now()
	if now.Sub(r.lastEval) < evalInterval {
		return nil, decide.Request{}, decide.Response{}, false
	}
	r.lastEval = now

	// One ledger snapshot per ranking: the no-shield filter and the state built
	// below read the same snapshot (#1870, UC-040 A33).
	charged, noShield := r.shieldLocked()
	ranked, weights, fitEval := r.rankLocked(c, now, noShield)
	r.lastFitness = fitEval

	// Emission is bounded to maxDecisionsPerEval per evaluation. The weighted
	// per-minute budget (policy.max_interventions_per_minute) is NOT enforced
	// here any more: the unified rate limiter moved to the decision loop's
	// permission chain (ratelimit.go) so the budget is spent only on decisions
	// that survive the allow-list and battery gates. cd.cost is still used by
	// scoreAndSort for deterministic cheaper-first tie-breaking.
	var out []Decision
	for _, sc := range ranked {
		if len(out) == maxDecisionsPerEval {
			break
		}
		cd := sc.candidate
		d := cd.decision
		d.Objectives = weights
		out = append(out, d)
		if cd.difficulty {
			// Difficulty interventions invalidate the movement-variance
			// baseline: players change behavior in response (#722 §5).
			r.lastDifficultyAt = now
		}
	}

	if build == nil {
		return out, decide.Request{}, decide.Response{}, true
	}
	// #1295: the rules-path answers describe the decision Evaluate actually emits
	// (out[0] == ranked[0]): the questions are widened to the engine's own ranked
	// interventions and targets, so the allow-list (applied afterwards by the
	// permission chain) cannot filter the answers into a different choice.
	req := emissionRequest(build(charged), ranked)
	return out, req, answerFromRanked(ranked, req, true), true
}

// RecordActed arms the variance cooldown for a decision some OTHER backend made
// and the loop dispatched (#1226): a remote backend's difficulty intervention
// invalidates the movement-variance baseline exactly like a rules one, so the
// chaos rules must stay quiet for one window. The rules engine's own emissions
// arm it inside Evaluate.
func (r *ObjectiveRules) RecordActed(d Decision) {
	if !difficultyInterventions[d.Intervention] {
		return
	}
	r.mu.Lock()
	r.lastDifficultyAt = r.now()
	r.mu.Unlock()
}

// difficultyInterventions are the interventions that change difficulty or
// movement demand (the rule set marks its own candidates with difficulty=true;
// this is the name-level view for decisions made elsewhere). They arm the
// variance cooldown when dispatched.
var difficultyInterventions = map[string]bool{
	InterventionAdjustMusicTempo:        true,
	InterventionRampTempo:               true,
	InterventionAdjustPlayerSensitivity: true,
	InterventionAdjustGlobalSensitivity: true,
	InterventionAdjustGlobalDifficulty:  true,
	InterventionSetPacingProfile:        true,
	InterventionSetPlayerHandicap:       true,
}

// rankLocked runs the rule set against c and returns every candidate that
// survives the policy filters and minScore, scored and in emission order, plus
// the weights and fitness evaluation used. It is the side-effect-light core
// shared by Evaluate and the decide rules backend (decide_rules.go): it draws
// R9's chaos roll from r.rng but does NOT touch lastEval, lastFitness or
// lastDifficultyAt — those belong to the caller that actually emits. noShield is
// the ranking's shield-ledger snapshot: the players a grant_shield would be refused
// for (nil: nobody). Caller holds r.mu.
func (r *ObjectiveRules) rankLocked(c gamecontext.GameContext, now time.Time, noShield map[string]bool) ([]scoredCandidate, map[string]float64, FitnessEvaluation) {
	weights := normalizedWeights(r.objectives.Objectives())
	pol := r.policy.Policy()
	fit := r.fitness.Fitness()

	// Evaluate the per-objective fitness functions against the live context and
	// the flag-resolved thresholds (#731). The evaluation both steers selection
	// (failing fitness amplifies the candidates serving that objective) and is
	// retained by Evaluate for the span's cycle-level fitness.evaluated attribute.
	fitEval := EvaluateFitness(c, fit)

	// R9's randomness is drawn here so the rule functions stay pure.
	roll, rollTarget := r.chaosRoll(c)

	cands := enduranceCandidates(c, fit)
	cands = append(cands, balancedCandidates(c, fit)...)
	cands = append(cands, accelerateCandidates(c, fit, weights)...)
	cands = append(cands, chaosCandidates(c, pol, roll, rollTarget)...)

	cands = applyBatteryPolicy(cands, c, pol, weights)
	cands = applyChargePolicy(cands, noShield)
	if r.appliedSource != nil {
		cands = applyAppliedPolicy(cands, r.appliedSource(), c)
	}
	cands = r.applyVarianceCooldown(cands, now, pol)
	return scoreAndSort(cands, weights, fitEval), weights, fitEval
}

// chaosRoll draws R9's randomness: a roll in [0,1) and a random active player.
// Caller holds r.mu (rand.Rand is not goroutine-safe).
func (r *ObjectiveRules) chaosRoll(c gamecontext.GameContext) (float64, string) {
	active := activePlayers(c)
	if len(active) == 0 {
		return 0, ""
	}
	r.maybeSeedLocked()
	r.chaosDraws++
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

// applyChargePolicy is the shared no-shield filter (UC-040 A31, A33; #1870, #2044):
// it drops every grant_shield candidate whose target is in noShield (charged, at the
// shield cap or in the cooldown), because the ledger would refuse it. Like the
// battery policy the candidate is not ranked, so R4 does not take an emission slot
// every second for a refused shield. It does not move the shield to another player:
// R4 means "the weakest". A nil or empty noShield keeps every candidate.
func applyChargePolicy(cands []candidate, noShield map[string]bool) []candidate {
	if len(noShield) == 0 {
		return cands
	}
	kept := cands[:0]
	for _, cd := range cands {
		if cd.decision.Intervention == InterventionGrantShield && noShield[cd.decision.TargetSerial] {
			continue
		}
		kept = append(kept, cd)
	}
	return kept
}

// applyAppliedPolicy is the rules' already-applied filter (UC-040 A35 step 4,
// #2046): it drops a state or level candidate whose value equals the one the loop
// last wrote for the same intervention and target (applied, from one ledger snapshot
// per ranking), as the permission chain would block it already_applied. Like the
// no-shield filter the candidate is not ranked, so a suppressed level does not take
// an emission slot every second; another candidate may rank instead. A target that
// left the game keeps its candidate (the chain drops that entry). nil or empty
// applied keeps every candidate.
func applyAppliedPolicy(cands []candidate, applied map[string]string, c gamecontext.GameContext) []candidate {
	if len(applied) == 0 {
		return cands
	}
	kept := cands[:0]
	for _, cd := range cands {
		d := cd.decision
		if v, ok := applied[appliedKey(d)]; ok && idempotentInterventions[d.Intervention] &&
			present(c, d.TargetSerial) && v == normalisedValue(d.Value) {
			continue
		}
		kept = append(kept, cd)
	}
	return kept
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

// applyVarianceCooldown enforces Policy.MovementVarianceWindow (10s): after any
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

// scoreAndSort scores candidates (urgency x weight[objective] x fitness
// amplification), drops those below minScore, and orders them deterministically:
// higher score first, then cheaper/safer, then lexicographic intervention and
// target.
//
// Fitness amplification (#731): a failing fitness function makes the candidates
// serving its objective more urgent. The multiplier is 1 + pressure, where
// pressure is 0 for a satisfied (or unevaluated, or fitness-less like chaos)
// objective and up to 1 for a maximally failing one — so a failing endurance
// fitness up to doubles the effective urgency of endurance candidates, which can
// flip the selected winner between objectives without ever blocking a candidate
// outright.
func scoreAndSort(cands []candidate, weights map[string]float64, fit FitnessEvaluation) []scoredCandidate {
	kept := make([]scoredCandidate, 0, len(cands))
	for _, cd := range cands {
		amp := 1 + fit.Pressure(cd.objective)
		s := clamp01(cd.urgency) * weights[cd.objective] * amp
		if s < minScore {
			continue
		}
		kept = append(kept, scoredCandidate{cd, s})
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
	return kept
}

// scoredCandidate is a candidate that survived scoreAndSort, with the score it
// was ranked by (urgency x weight x fitness amplification, >= minScore).
type scoredCandidate struct {
	candidate
	score float64
}
