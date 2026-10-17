package decision

import (
	"context"
	"slices"
	"sort"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/gamecontext"
)

// effect_metric_signals_test.go pins #1444: agent_intervention_effect_delta carries
// only the 0..1 progress-type signals, so a quantile across signals is meaningful.
// The elapsed-time (`*_seconds`) and constant-threshold keys stay on the
// agent.intervention.effect spans.

// wantEffectMetricSignals are the fitness keys whose delta goes to the metric.
var wantEffectMetricSignals = []string{
	"accelerate.session_progress",
	"balanced.skill_gap",
	"balanced.skill_gap_progress",
	"balanced.spike_survival_progress",
	"balanced.spike_survival_ratio",
	"endurance.session_progress",
}

// wantEffectSpanOnlySignals are the fitness keys recorded on the spans only.
var wantEffectSpanOnlySignals = []string{
	"accelerate.session_seconds",
	"accelerate.target_session_seconds",
	"balanced.max_skill_gap",
	"balanced.spike_survival_threshold",
	"endurance.min_session_seconds",
	"endurance.session_seconds",
}

// allObjectivesContext is an active game in which every fitness function runs and
// emits every key: a known session duration and two players with a skill level, a
// peak accel and a movement variance.
func allObjectivesContext(gameID string, durSeconds, skillA, skillB float64) gamecontext.GameContext {
	player := func(skill float64) *gamecontext.PlayerSignals {
		return &gamecontext.PlayerSignals{
			SkillLevel:                fp(skill),
			PeakAccel:                 fp(2),
			MovementVarianceAggregate: fp(0.04),
		}
	}
	c := durationContext(gameID, durSeconds)
	c.Players = map[string]*gamecontext.PlayerSignals{"A": player(skillA), "B": player(skillB)}
	return c
}

// UC: UC-035 BR-004
func TestEffect_MetricSignalsClassifyEveryFitnessKey(t *testing.T) {
	keys := EvaluateFitness(allObjectivesContext("g", 30, 0.2, 0.7), defaultFit()).Evaluated()

	var metric, spanOnly []string
	for k := range keys {
		if effectMetricSignal(k) {
			metric = append(metric, k)
		} else {
			spanOnly = append(spanOnly, k)
		}
	}
	sort.Strings(metric)
	sort.Strings(spanOnly)
	// A new fitness key fails here until it is added to one of the two lists: decide
	// whether its delta shares the 0..1 unit of the metric before it lands there.
	if !slices.Equal(metric, wantEffectMetricSignals) {
		t.Errorf("metric signals = %v, want %v", metric, wantEffectMetricSignals)
	}
	if !slices.Equal(spanOnly, wantEffectSpanOnlySignals) {
		t.Errorf("span-only signals = %v, want %v", spanOnly, wantEffectSpanOnlySignals)
	}
}

// UC: UC-035 BR-004
func TestEffect_MetricOmitsSecondsAndThresholdKeys(t *testing.T) {
	provider := newFakeContextProvider()
	const gameID = "game_1444"
	baseline := EvaluateFitness(allObjectivesContext(gameID, 30, 0.2, 0.7), defaultFit()).Evaluated()
	provider.set(gameID, allObjectivesContext(gameID, 50, 0.3, 0.5))

	l, sr, reader, sched := effectLoop(t, provider, gameID)
	if id := l.schedule(shieldDecision(), baseline, defaultFit()); id == "" {
		t.Fatal("no effect sample scheduled")
	}
	sched.fire()
	l.AwaitEffects()

	// The metric carries exactly the progress-type signals.
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("metric collect: %v", err)
	}
	var recorded []string
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_intervention_effect_delta" {
				continue
			}
			for _, dp := range m.Data.(metricdata.Histogram[float64]).DataPoints {
				v, _ := dp.Attributes.Value(attribute.Key(AttrEffectSignal))
				recorded = append(recorded, v.AsString())
			}
		}
	}
	sort.Strings(recorded)
	if !slices.Equal(recorded, wantEffectMetricSignals) {
		t.Fatalf("agent_intervention_effect_delta signals = %v, want %v", recorded, wantEffectMetricSignals)
	}
	for _, s := range recorded {
		if strings.HasSuffix(s, "_seconds") || strings.HasSuffix(s, "_threshold") || s == "balanced.max_skill_gap" {
			t.Errorf("metric records span-only signal %q", s)
		}
	}

	// The spans still carry every key, the span-only ones included.
	var spanned []string
	for _, s := range sr.Ended() {
		if s.Name() != SpanInterventionEffect {
			continue
		}
		if v, ok := attrMap(s.Attributes())[AttrEffectSignal].(string); ok {
			spanned = append(spanned, v)
		}
	}
	sort.Strings(spanned)
	want := append(slices.Clone(wantEffectMetricSignals), wantEffectSpanOnlySignals...)
	sort.Strings(want)
	if !slices.Equal(spanned, want) {
		t.Errorf("effect span signals = %v, want %v", spanned, want)
	}
}
