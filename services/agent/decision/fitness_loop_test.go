package decision

import (
	"context"
	"io"
	"log/slog"
	"math/rand/v2"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// liveEngine builds an ObjectiveRules over LiveObjectives + LiveFitness sources
// with an injected clock, so the loop can publish flag-driven thresholds each
// cycle and tests can step the engine's eval interval deterministically.
func liveEngine(weights map[string]float64, clock *time.Time) *ObjectiveRules {
	now := func() time.Time { return *clock }
	rng := rand.New(rand.NewPCG(1, 2))
	r := newObjectiveRules(NewLiveObjectives(), DefaultStaticConfig(), NewLiveFitness(), now, rng)
	r.SetObjectives(weights)
	return r
}

// loopWithEngine wires a recording-tracer loop over a mutable flag source and
// the given real rules engine (so the fitness publish/read seams run).
func loopWithEngine(t *testing.T, fl *settableFlags, engine RulesEngine) (*Loop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l := NewLoop(fl, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.Rules = engine
	l.Actions = &fakeSink{}
	return l, sr
}

// spanFitness extracts the fitness.evaluated k=v slice from the decision span.
func spanFitness(t *testing.T, sr *tracetest.SpanRecorder) []string {
	t.Helper()
	decs := spansByName(sr.Ended(), SpanDecision)
	if len(decs) == 0 {
		t.Fatal("no decision span emitted")
	}
	v, ok := attrValue(decs[len(decs)-1], AttrFitnessEvaluated)
	if !ok {
		t.Fatal("decision span missing fitness.evaluated")
	}
	return v.AsStringSlice()
}

// fitnessValue scans a k=v slice for a key and returns its string value.
func fitnessValue(kvs []string, key string) (string, bool) {
	for _, kv := range kvs {
		if len(kv) > len(key)+1 && kv[:len(key)] == key && kv[len(key)] == '=' {
			return kv[len(key)+1:], true
		}
	}
	return "", false
}

// firingCtx is a young session (20s) with one elimination — the endurance rule
// always fires here, so a decision (and thus a decision span) is emitted every
// cycle regardless of the accelerate threshold under test. The fitness functions
// still evaluate every objective, so accelerate fitness rides the span too.
func firingCtx() gamecontext.GameContext {
	c := ctxWithDuration(fp(20))
	c.Session.EliminationSequence = []string{"GONE"}
	c.Players["A"] = &gamecontext.PlayerSignals{Serial: "A", Active: bp(true)}
	return c
}

func enduranceFlags() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		Objectives:           map[string]float64{ObjectiveEndurance: 1.0},
		InterventionsAllowed: []string{"adjust_music_tempo", "play_audio_cue"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		Fitness: flags.Fitness{
			EnduranceMinSessionSeconds:     120,
			BalancedMaxSkillGap:            0.4,
			BalancedSpikeSurvivalThreshold: 0.8,
			AccelerateTargetSessionSeconds: 60,
		},
	}
}

// TestLoop_FitnessLiftedOntoSpan: a cycle that emits a decision carries the
// cycle-level fitness.evaluated values (dotted keys) on the decision span (#731).
func TestLoop_FitnessLiftedOntoSpan(t *testing.T) {
	clock := time.Unix(10_000, 0)
	engine := liveEngine(map[string]float64{ObjectiveEndurance: 1.0}, &clock)
	fl := &settableFlags{snap: enduranceFlags()}
	l, sr := loopWithEngine(t, fl, engine)

	l.OnEvaluate(context.Background(), firingCtx(), testTrigger())

	kvs := spanFitness(t, sr)
	if v, ok := fitnessValue(kvs, "endurance.min_session_seconds"); !ok || v != "120" {
		t.Errorf("endurance.min_session_seconds = %q (present=%v), want 120", v, ok)
	}
	if v, ok := fitnessValue(kvs, "endurance.session_seconds"); !ok || v != "20" {
		t.Errorf("endurance.session_seconds = %q (present=%v), want 20", v, ok)
	}
	if v, ok := fitnessValue(kvs, "accelerate.target_session_seconds"); !ok || v != "60" {
		t.Errorf("accelerate.target_session_seconds = %q (present=%v), want 60", v, ok)
	}
}

// TestLoop_MidSessionFlagChangeChangesOutcome is the #731 runtime-tunability
// acceptance test: with NOTHING changing but the accelerate target flag between
// two cycles, the evaluated fitness on the span changes — proving the threshold
// is read from the flag every cycle, not cached.
func TestLoop_MidSessionFlagChangeChangesOutcome(t *testing.T) {
	clock := time.Unix(10_000, 0)
	engine := liveEngine(map[string]float64{ObjectiveEndurance: 1.0}, &clock)

	base := enduranceFlags()
	fl := &settableFlags{snap: base}
	l, sr := loopWithEngine(t, fl, engine)

	c := firingCtx() // 20s session, unchanged across cycles

	// Cycle 1: endurance min 120s -> 20/120 -> session_progress ~0.1667.
	l.OnEvaluate(context.Background(), c, testTrigger())
	prog1, _ := fitnessValue(spanFitness(t, sr), "endurance.session_progress")
	if prog1 == "" {
		t.Fatal("cycle 1 missing endurance.session_progress")
	}

	// Mid-session flag change: lower the endurance minimum to 40s. Step the
	// engine clock past evalInterval so the rules re-run.
	flChanged := base
	flChanged.Fitness.EnduranceMinSessionSeconds = 40
	fl.snap = flChanged
	advance(&clock, 2*time.Second)

	// Cycle 2: SAME 20s session, but min is now 40s -> 20/40 -> session_progress
	// 0.5. The evaluation changed solely because the flag changed (never cached).
	l.OnEvaluate(context.Background(), c, testTrigger())
	kvs2 := spanFitness(t, sr)
	min2, _ := fitnessValue(kvs2, "endurance.min_session_seconds")
	prog2, _ := fitnessValue(kvs2, "endurance.session_progress")
	if min2 != "40" {
		t.Errorf("cycle 2 endurance.min_session_seconds = %q, want 40 (flag changed)", min2)
	}
	if prog2 != "0.5" {
		t.Errorf("cycle 2 endurance.session_progress = %q, want 0.5", prog2)
	}
	if prog1 == prog2 {
		t.Errorf("mid-session flag change did not change the evaluated fitness (%q == %q)", prog1, prog2)
	}
}
