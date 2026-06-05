package decision

import (
	"context"
	"math/rand/v2"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// newTestEngine builds an ObjectiveRules with injected clock, fixed-seed rng,
// and the given config.
func newTestEngine(cfg StaticConfig, clock *time.Time) *ObjectiveRules {
	now := func() time.Time { return *clock }
	rng := rand.New(rand.NewPCG(1, 2))
	return newObjectiveRules(cfg, cfg, cfg, now, rng)
}

func advance(clock *time.Time, d time.Duration) { *clock = clock.Add(d) }

func TestObjectiveRules_ConflictResolutionByWeights(t *testing.T) {
	// Acceptance scenario: weights {endurance:0.7, balanced:0.3}.
	// Context fires BOTH an endurance rule (young session, eliminations) and a
	// balanced rule (large skill gap) — the weights resolve the conflict.
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = map[string]float64{ObjectiveEndurance: 0.7, ObjectiveBalanced: 0.3}
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.2), active: true},
		"STRONG": {skill: fptr(0.9), active: true}, // gap 0.7 > 0.4
	})

	out := r.Evaluate(context.Background(), c)
	if len(out) == 0 {
		t.Fatal("expected decisions")
	}
	if out[0].ObjectiveServed != ObjectiveEndurance {
		t.Fatalf("top decision serves %q, want endurance (0.7 weight wins)", out[0].ObjectiveServed)
	}
	for _, d := range out {
		if len(d.Objectives) != 2 || d.Objectives[ObjectiveEndurance] != 0.7 {
			t.Errorf("decision must carry the active weights, got %v", d.Objectives)
		}
		if len(d.Fitness) == 0 {
			t.Errorf("decision %s must carry evaluated fitness values", d.Intervention)
		}
	}
}

func TestObjectiveRules_DefaultObjectivesWhenSourceEmpty(t *testing.T) {
	// Acceptance: works with {endurance: 1.0} when no flag value is provided.
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = nil // "no flag value"
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	// Long session with a big skill gap: accelerate + balanced rules fire, but
	// with endurance-only weights they all score 0 and must be dropped.
	c := gameCtx(200, nil, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.2), intensity: fptr(0.3), active: true},
		"MID":    {skill: fptr(0.5), intensity: fptr(0.8), active: true},
		"STRONG": {skill: fptr(0.9), intensity: fptr(1.5), active: true},
	})
	if out := r.Evaluate(context.Background(), c); len(out) != 0 {
		t.Fatalf("decisions = %v, want none (non-endurance candidates must score 0)", out)
	}

	// An endurance-relevant context still decides.
	advance(&clock, 2*time.Second)
	c2 := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"A": {active: true}, "B": {active: true},
	})
	out := r.Evaluate(context.Background(), c2)
	if len(out) == 0 || out[0].ObjectiveServed != ObjectiveEndurance {
		t.Fatalf("decisions = %v, want endurance decision under default weights", out)
	}
	if got := out[0].Objectives[ObjectiveEndurance]; got != 1.0 {
		t.Errorf("objectives = %v, want default {endurance:1.0}", out[0].Objectives)
	}
}

func TestObjectiveRules_CadenceGate(t *testing.T) {
	cfg := DefaultStaticConfig()
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"A": {active: true}, "B": {active: true},
	})

	if out := r.Evaluate(context.Background(), c); len(out) == 0 {
		t.Fatal("first evaluation must decide")
	}
	advance(&clock, 200*time.Millisecond)
	if out := r.Evaluate(context.Background(), c); out != nil {
		t.Fatalf("decisions = %v, want nil within the 1s cadence gate", out)
	}
	advance(&clock, time.Second)
	// Past the gate the engine runs again (budget may or may not admit).
	r.Evaluate(context.Background(), c) // must not panic; gate reopened
}

func TestObjectiveRules_MaxTwoDecisions(t *testing.T) {
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = map[string]float64{ObjectiveEndurance: 1.0, ObjectiveBalanced: 1.0}
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	// Young dying session with skill gap: endurance tempo (1) + cue (0.5) +
	// balanced sensitivity (1) + shield (1) candidates available — more than the
	// per-evaluation cap, so emission is bounded to maxDecisionsPerEval.
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.1), active: true},
		"STRONG": {skill: fptr(0.9), active: true},
	})

	out := r.Evaluate(context.Background(), c)
	if len(out) != maxDecisionsPerEval {
		t.Fatalf("decisions = %d, want capped at %d", len(out), maxDecisionsPerEval)
	}

	// NOTE: the weighted per-minute *budget* is no longer enforced by the engine
	// (the reconciled #727/#728 stack moved the unified rate limiter to the
	// decision loop's permission chain — see layers_test.go's RateLimit* tests).
	// The engine only caps per-evaluation emission; across cycles, after the 1s
	// cadence gate, it can decide again.
	advance(&clock, 2*time.Second)
	if out := r.Evaluate(context.Background(), c); len(out) != maxDecisionsPerEval {
		t.Fatalf("decisions = %d, want %d again (no engine-level budget)", len(out), maxDecisionsPerEval)
	}
}

func TestObjectiveRules_DeterministicOrdering(t *testing.T) {
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = map[string]float64{ObjectiveEndurance: 0.7, ObjectiveBalanced: 0.3}
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.2), active: true},
		"STRONG": {skill: fptr(0.9), active: true},
	})

	var first []Decision
	for i := 0; i < 5; i++ {
		clock := time.Unix(10_000, 0)
		r := newTestEngine(cfg, &clock)
		out := r.Evaluate(context.Background(), c)
		if i == 0 {
			first = out
			continue
		}
		if len(out) != len(first) {
			t.Fatalf("run %d: %d decisions, first run had %d", i, len(out), len(first))
		}
		for j := range out {
			if out[j].Intervention != first[j].Intervention || out[j].TargetSerial != first[j].TargetSerial {
				t.Fatalf("run %d decision %d = %+v, first run had %+v (must be deterministic)",
					i, j, out[j], first[j])
			}
		}
	}
}

func TestObjectiveRules_VarianceCooldownAfterDifficulty(t *testing.T) {
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = map[string]float64{ObjectiveEndurance: 0.5, ObjectiveChaos: 0.5}
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	// First evaluation: young dying session -> endurance tempo (difficulty)
	// admitted, which starts the variance cooldown.
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"A": {variance: fptr(0.01), active: true},
		"B": {active: true},
	})
	out := r.Evaluate(context.Background(), c)
	emittedTempo := false
	for _, d := range out {
		if d.Intervention == InterventionAdjustMusicTempo {
			emittedTempo = true
		}
	}
	if !emittedTempo {
		t.Fatalf("decisions = %v, want the tempo difficulty decision emitted (starts the cooldown)", out)
	}

	// Inside the cooldown window chaos/variance candidates are suppressed.
	advance(&clock, 2*time.Second)
	for _, d := range r.Evaluate(context.Background(), c) {
		if d.ObjectiveServed == ObjectiveChaos {
			t.Fatalf("chaos decision %v emitted inside the variance cooldown", d.Intervention)
		}
	}
}

func TestObjectiveRules_ConcurrentEvaluate(t *testing.T) {
	cfg := DefaultStaticConfig()
	r := NewObjectiveRules(&cfg) // real clock + rng
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"A": {active: true}, "B": {active: true},
	})

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.Evaluate(context.Background(), c)
		}()
	}
	wg.Wait() // -race validates the engine's locking
}

func TestObjectiveRules_EmptyContext(t *testing.T) {
	cfg := DefaultStaticConfig()
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)
	if out := r.Evaluate(context.Background(), gamecontext.GameContext{}); out != nil {
		t.Fatalf("decisions = %v, want nil on an empty context", out)
	}
}

func TestObjectiveRules_ZeroThresholdsSafe(t *testing.T) {
	// A misbehaving flag source could deliver zero thresholds — the engine
	// must not divide by zero or emit garbage.
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = map[string]float64{
		ObjectiveEndurance: 1, ObjectiveBalanced: 1, ObjectiveAccelerate: 1, ObjectiveChaos: 1,
	}
	cfg.FitnessThresholds = FitnessThresholds{} // all zero
	cfg.PolicyValues = Policy{}                 // all zero (battery policy off, budget zero)
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	c := gameCtx(100, []string{"GONE"}, map[string]testPlayer{
		"A": {skill: fptr(0.2), intensity: fptr(0.3), active: true},
		"B": {skill: fptr(0.9), intensity: fptr(1.5), active: true},
	})
	out := r.Evaluate(context.Background(), c) // must not panic on zero thresholds
	// The engine no longer enforces the per-minute budget (the reconciled
	// #727/#728 stack moved the unified rate limiter to the decision loop — a
	// zero budget there is what admits nothing; see layers_test.go). The engine's
	// contract here is "do not panic / divide by zero", and it still caps
	// emission at maxDecisionsPerEval.
	if len(out) > maxDecisionsPerEval {
		t.Fatalf("decisions = %v, want at most %d", out, maxDecisionsPerEval)
	}
	for _, d := range out {
		if d.Intervention == "" {
			t.Errorf("emitted a decision with no intervention: %+v", d)
		}
	}
}

func TestObjectiveRules_WeightsMapIsCopied(t *testing.T) {
	// A live map from a future OpenFeature source (#727) must not be shared
	// with emitted decisions: the engine hands out a copy.
	live := map[string]float64{ObjectiveEndurance: 1.0}
	cfg := DefaultStaticConfig()
	cfg.ObjectiveWeights = live
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)

	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"A": {active: true}, "B": {active: true},
	})
	out := r.Evaluate(context.Background(), c)
	if len(out) == 0 {
		t.Fatal("expected decisions")
	}
	live[ObjectiveEndurance] = 0.123 // source mutates its map afterwards
	if out[0].Objectives[ObjectiveEndurance] != 1.0 {
		t.Fatalf("decision weights = %v, want a snapshot unaffected by source mutation", out[0].Objectives)
	}
}

// TestObjectiveRules_FitnessAmplifiesWinner verifies #731's core selection
// effect: with two objectives weighted EQUALLY and both firing a candidate, the
// objective whose fitness is failing wins because its candidate's urgency is
// amplified by the failing-fitness pressure. The same candidate set with that
// objective's fitness satisfied lets the other objective win instead.
func TestObjectiveRules_FitnessAmplifiesWinner(t *testing.T) {
	// Equal weights so the base scores tie and fitness pressure is the tiebreaker.
	weights := map[string]float64{ObjectiveEndurance: 0.5, ObjectiveBalanced: 0.5}

	// A context that fires BOTH an endurance candidate (young session with
	// eliminations) and a balanced candidate (skill gap 0.6 > the 0.4 max). The
	// gap is chosen so the balanced score sits BETWEEN endurance's amplified
	// (failing) and un-amplified (satisfied) scores — so the endurance fitness
	// state alone decides the winner.
	c := gameCtx(20, []string{"GONE"}, map[string]testPlayer{
		"WEAK":   {skill: fptr(0.2), active: true},
		"STRONG": {skill: fptr(0.8), active: true}, // gap 0.6 > 0.4
	})

	// Case 1: endurance threshold high (120s) -> a 20s session is deep in
	// endurance failure -> strong endurance pressure -> endurance wins.
	cfgFail := DefaultStaticConfig()
	cfgFail.ObjectiveWeights = weights
	clock1 := time.Unix(10_000, 0)
	rFail := newTestEngine(cfgFail, &clock1)
	failOut := rFail.Evaluate(context.Background(), c)
	if len(failOut) == 0 {
		t.Fatal("expected decisions (failing endurance)")
	}
	if failOut[0].ObjectiveServed != ObjectiveEndurance {
		t.Fatalf("failing endurance fitness must amplify endurance to the top, got %q", failOut[0].ObjectiveServed)
	}

	// Case 2: endurance threshold lowered below the session age (10s) so the 20s
	// session SATISFIES endurance -> zero endurance pressure. Balance still fails
	// (gap 0.8 > 0.4) so balanced pressure now dominates -> balanced wins. Same
	// candidate set, fitness flips the winner.
	cfgPass := DefaultStaticConfig()
	cfgPass.ObjectiveWeights = weights
	cfgPass.FitnessThresholds.EnduranceMinSessionSeconds = 10
	clock2 := time.Unix(10_000, 0)
	rPass := newTestEngine(cfgPass, &clock2)
	passOut := rPass.Evaluate(context.Background(), c)
	if len(passOut) == 0 {
		t.Fatal("expected decisions (satisfied endurance)")
	}
	if passOut[0].ObjectiveServed != ObjectiveBalanced {
		t.Fatalf("satisfied endurance + failing balance must let balanced win, got %q", passOut[0].ObjectiveServed)
	}
}

// TestObjectiveRules_LastFitnessExposed verifies the engine retains the cycle's
// fitness evaluation for the loop to lift onto the span (#731), under the dotted
// key vocabulary.
func TestObjectiveRules_LastFitnessExposed(t *testing.T) {
	cfg := DefaultStaticConfig()
	clock := time.Unix(10_000, 0)
	r := newTestEngine(cfg, &clock)
	c := gameCtx(30, nil, map[string]testPlayer{"A": {active: true}})

	_ = r.Evaluate(context.Background(), c)
	vals := r.LastFitness().Evaluated()
	if vals["endurance.session_seconds"] != 30 {
		t.Errorf("endurance.session_seconds = %v, want 30", vals["endurance.session_seconds"])
	}
	if vals["accelerate.target_session_seconds"] != 60 {
		t.Errorf("accelerate.target_session_seconds = %v, want 60", vals["accelerate.target_session_seconds"])
	}
}
