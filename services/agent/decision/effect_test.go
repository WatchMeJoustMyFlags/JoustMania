package decision

import (
	"context"
	"io"
	"log/slog"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.uber.org/goleak"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// effect_test.go drives the #918 intervention-effect MEASUREMENT loop end-to-end with
// a FAKE context provider and a MANUAL follow-up scheduler — no real sleeps. It covers:
//   - baseline stamp + sample scheduled at dispatch (the id rides the decision span);
//   - delta emission after the window (span attributes + the metric);
//   - the CANCELLATION/eviction case (game gone before the window -> aborted record, no
//     metric, no leaked goroutine);
//   - shutdown cancellation mid-window (rootCtx cancelled -> sampler returns, no emit, no
//     leak), asserted under goleak.

// manualScheduler is the afterFunc test seam: it captures the fn the sampler scheduled
// so the test can fire the window deterministically (no real timer). stop() is a no-op
// (no real timer to stop); it must be concurrency-safe because the shutdown-cancel path
// can call several samplers' stops at once.
type manualScheduler struct {
	fn func()
}

func (m *manualScheduler) after(_ time.Duration, fn func()) func() bool {
	m.fn = fn
	return func() bool { return true }
}

// fire invokes the captured follow-up fn (the window elapsing).
func (m *manualScheduler) fire() {
	if m.fn != nil {
		m.fn()
	}
}

// effectLoop builds a Loop wired for effect measurement against a fake provider, with a
// recording tracer and a manual metric reader. It returns the loop, span recorder,
// reader, and the manual scheduler driving the follow-up timer.
func effectLoop(t *testing.T, provider ContextProvider, gameID string) (*Loop, *tracetest.SpanRecorder, *metric.ManualReader, *manualScheduler) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	l := NewLoop(&settableFlags{snap: permissiveEffectSnapshot()}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.effectDelta = newEffectDeltaHistogram(mp)
	l.SetGameID(gameID)
	l.SetContextProvider(provider)
	l.SetRootContext(context.Background())

	sched := &manualScheduler{}
	l.afterFunc = sched.after
	return l, sr, reader, sched
}

// permissiveEffectSnapshot enables the agent with a permissive allow-list and rate
// budget so a dispatched decision is not blocked.
func permissiveEffectSnapshot() flags.Snapshot {
	return flags.Snapshot{
		Enabled:              true,
		Mode:                 flags.DefaultMode,
		InterventionsAllowed: []string{"grant_shield"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
		Fitness: flags.Fitness{
			EnduranceMinSessionSeconds:     120,
			BalancedMaxSkillGap:            0.4,
			BalancedSpikeSurvivalThreshold: 0.8,
			AccelerateTargetSessionSeconds: 60,
		},
	}
}

// enduranceBaseline is a fitness.evaluated-shaped baseline for the endurance objective.
func enduranceBaseline() map[string]float64 {
	return map[string]float64{
		"endurance.min_session_seconds": 120,
		"endurance.session_seconds":     30,
		"endurance.session_progress":    0.25,
	}
}

// durationContext builds an active game context whose only fitness signal is endurance,
// driven by the session duration, so the follow-up EvaluateFitness produces a comparable
// endurance.session_progress.
func durationContext(gameID string, durSeconds float64) gamecontext.GameContext {
	d := durSeconds
	return gamecontext.GameContext{
		SessionID: gameID,
		GameKind:  "real",
		Session:   gamecontext.SessionSignals{GameActive: bp(true), DurationSeconds: &d},
		Players:   map[string]*gamecontext.PlayerSignals{},
	}
}

func shieldDecision() Decision {
	return Decision{Intervention: "grant_shield", ObjectiveServed: ObjectiveEndurance, Reason: "test"}
}

// ---- baseline stamp + delta emission ----

func TestEffect_DeltaEmittedAfterWindow(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_abc"
	// At follow-up the session has run to 90s of a 120s target: progress 0.75 vs the
	// 0.25 baseline -> delta +0.5.
	provider.set(gameID, durationContext(gameID, 90))

	l, sr, reader, sched := effectLoop(t, provider, gameID)

	id := l.scheduleEffectSample(shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("scheduleEffectSample returned empty id; expected a scheduled sample")
	}
	sched.fire()
	l.AwaitEffects()

	// Span: find the per-signal effect span carrying the delta.
	var found bool
	for _, s := range sr.Ended() {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		attrs := attrMap(s.Attributes())
		if attrs[AttrEffectSignal] != "endurance.session_progress" {
			continue
		}
		found = true
		if got := attrs[AttrEffectInterventionID]; got != id {
			t.Errorf("effect span intervention id = %q, want %q", got, id)
		}
		if got := attrs[AttrEffectBaseline]; got != float64Attr(0.25) {
			t.Errorf("baseline attr = %v, want 0.25", got)
		}
		if got := attrs[AttrEffectFollowUp]; got != float64Attr(0.75) {
			t.Errorf("follow_up attr = %v, want 0.75", got)
		}
		if got := attrs[AttrEffectDelta]; got != float64Attr(0.5) {
			t.Errorf("delta attr = %v, want 0.5", got)
		}
	}
	if !found {
		t.Fatal("no agent.intervention.effect span for endurance.session_progress")
	}

	// Metric: the delta histogram recorded a point for the session_progress signal with
	// the right delta (a point is recorded per evaluated signal; check the one we assert).
	delta := readEffectDelta(t, reader, "endurance.session_progress")
	if delta != 0.5 {
		t.Errorf("recorded metric delta for session_progress = %v, want 0.5", delta)
	}
}

// the decision span carries the effect id when a sample is scheduled at dispatch.
func TestEffect_DecisionSpanCarriesInterventionID(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_xyz"
	provider.set(gameID, durationContext(gameID, 60))

	l, sr, _, sched := effectLoop(t, provider, gameID)
	l.Rules = fakeRules{out: []Decision{shieldDecision()}}
	// Pre-stamp the cycle's fitness evaluation so OnEvaluate lifts a baseline.
	l.Rules = &baselineRules{out: []Decision{shieldDecision()}, fitness: enduranceBaseline()}

	l.OnEvaluate(context.Background(), durationContext(gameID, 30), EvalTrigger{Signal: "metrics", T0: time.Now()})

	var decisionSpan sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.Name() == SpanDecision {
			decisionSpan = s
		}
	}
	if decisionSpan == nil {
		t.Fatal("no agent.decision span emitted")
	}
	attrs := attrMap(decisionSpan.Attributes())
	if attrs[AttrDecisionInterventionID] == nil {
		t.Fatal("decision span missing decision.intervention_id; effect sample was not scheduled")
	}

	sched.fire()
	l.AwaitEffects()
}

// ---- cancellation: game evicted before the window ----

func TestEffect_AbortedWhenGameEvicted(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_gone"
	provider.evict(gameID) // partition gone at follow-up time

	l, sr, reader, sched := effectLoop(t, provider, gameID)

	id := l.scheduleEffectSample(shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("expected a scheduled sample")
	}
	sched.fire()
	l.AwaitEffects()

	var aborted bool
	for _, s := range sr.Ended() {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		if attrMap(s.Attributes())[AttrEffectAborted] == true {
			aborted = true
		}
	}
	if !aborted {
		t.Fatal("expected an aborted effect record when the game was evicted")
	}
	// No metric recorded for an unmeasurable follow-up.
	if got := readEffectDelta(t, reader, ""); got != 0 {
		t.Errorf("expected no metric for aborted sample, got sum %v", got)
	}
}

// ---- shutdown cancellation mid-window: no emit, no leak ----

func TestEffect_ShutdownCancelsSamplerNoLeak(t *testing.T) {
	// Ignore the openfeature event-listener goroutine: it is a process-global started by
	// the flags provider, owned by main()'s deferred shutdown — not the #918 sampler.
	defer goleak.VerifyNone(t,
		goleak.IgnoreTopFunction("github.com/open-feature/go-sdk/openfeature.newEventExecutor.(*eventExecutor).startEventListener.func1.1"),
		goleak.IgnoreTopFunction("github.com/open-feature/go-sdk/openfeature.(*eventExecutor).startEventListener.func1.1"),
	)

	provider := newFakeContextProvider()
	const gameID = "game_shutdown"
	provider.set(gameID, durationContext(gameID, 90))

	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	l := NewLoop(&settableFlags{snap: permissiveEffectSnapshot()}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	l.SetGameID(gameID)
	l.SetContextProvider(provider)

	ctx, cancel := context.WithCancel(context.Background())
	l.SetRootContext(ctx)

	id := l.scheduleEffectSample(shieldDecision(), enduranceBaseline(), defaultFit())
	if id == "" {
		t.Fatal("expected a scheduled sample")
	}

	// Shut down before the (real, default 20s) window elapses: the sampler must observe
	// ctx.Done() and return without emitting, and AwaitInflight must join it.
	cancel()
	l.AwaitInflight()

	for _, s := range sr.Ended() {
		if s.Name() == SpanInterventionEffect {
			t.Fatal("sampler emitted an effect span after shutdown cancellation")
		}
	}
}

// ---- bounded pending set ----

func TestEffect_PendingSetBounded(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_burst"
	provider.set(gameID, durationContext(gameID, 90))

	l, _, _, _ := effectLoop(t, provider, gameID)
	// The samplers select on this root; cancel it in cleanup to drain every blocked
	// goroutine (the manual scheduler never fires them), then join so nothing leaks.
	ctx, cancel := context.WithCancel(context.Background())
	l.SetRootContext(ctx)
	t.Cleanup(func() {
		cancel()
		l.AwaitEffects()
	})

	scheduled := 0
	for i := 0; i < maxPendingEffects+10; i++ {
		if l.scheduleEffectSample(shieldDecision(), enduranceBaseline(), defaultFit()) != "" {
			scheduled++
		}
	}
	if scheduled != maxPendingEffects {
		t.Errorf("scheduled %d samples, want cap %d", scheduled, maxPendingEffects)
	}
}

// ---- no-op paths ----

func TestEffect_NoProviderNoSchedule(t *testing.T) {
	l := NewLoop(&settableFlags{snap: permissiveEffectSnapshot()}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if id := l.scheduleEffectSample(shieldDecision(), enduranceBaseline(), defaultFit()); id != "" {
		t.Errorf("expected no schedule without a context provider, got id %q", id)
	}
}

func TestEffect_NoBaselineNoSchedule(t *testing.T) {
	provider := newFakeContextProvider()
	l, _, _, _ := effectLoop(t, provider, "g")
	if id := l.scheduleEffectSample(shieldDecision(), nil, defaultFit()); id != "" {
		t.Errorf("expected no schedule with an empty baseline, got id %q", id)
	}
}

// ---- helpers ----

func attrMap(kvs []attribute.KeyValue) map[attribute.Key]interface{} {
	m := make(map[attribute.Key]interface{}, len(kvs))
	for _, kv := range kvs {
		switch kv.Value.Type() {
		case attribute.STRING:
			m[kv.Key] = kv.Value.AsString()
		case attribute.BOOL:
			m[kv.Key] = kv.Value.AsBool()
		case attribute.FLOAT64:
			m[kv.Key] = kv.Value.AsFloat64()
		default:
			m[kv.Key] = kv.Value.Emit()
		}
	}
	return m
}

func float64Attr(v float64) interface{} { return v }

// readEffectDelta sums the recorded agent_intervention_effect_delta histogram points
// whose signal label matches; pass "" to sum across all signals.
func readEffectDelta(t *testing.T, reader *metric.ManualReader, signal string) float64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("metric collect: %v", err)
	}
	var sum float64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_intervention_effect_delta" {
				continue
			}
			h, ok := m.Data.(metricdata.Histogram[float64])
			if !ok {
				t.Fatalf("metric %s is not a float histogram", m.Name)
			}
			for _, dp := range h.DataPoints {
				if signal != "" {
					if v, present := dp.Attributes.Value(attribute.Key(AttrEffectSignal)); !present || v.AsString() != signal {
						continue
					}
				}
				sum += dp.Sum
			}
		}
	}
	return sum
}

// baselineRules is a rules engine that returns a fixed decision and reports a fixed
// fitness evaluation via LastFitness, so OnEvaluate lifts a baseline onto the LayerState.
type baselineRules struct {
	out     []Decision
	fitness map[string]float64
}

func (r *baselineRules) Evaluate(context.Context, gamecontext.GameContext) []Decision { return r.out }

func (r *baselineRules) LastFitness() FitnessEvaluation {
	return FitnessEvaluation{Results: map[string]FitnessResult{
		ObjectiveEndurance: {Objective: ObjectiveEndurance, Evaluated: true, Values: r.fitness},
	}}
}
