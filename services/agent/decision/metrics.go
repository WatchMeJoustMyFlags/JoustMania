package decision

import (
	"go.opentelemetry.io/otel"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
)

// metrics.go holds the decision loop's OTel instruments. Today that is the LLM
// call-gate counter (#847); the audit trace (#724) carries the rest of the
// loop's observability as spans.

// decisionMeterName scopes the decision loop's OTel instruments, mirroring
// actions/writer.go's meterName convention (module path + package).
const decisionMeterName = "github.com/joustmania/agent/decision"

// newLLMGatedCounter builds the agent_llm_gated_total counter from the given meter
// provider. It is incremented once per gated remote decide cycle or retro, labeled
// reason=<gate reason> (llm_not_eligible / decide_interval / llm_budget_exhausted),
// so the gate's effect is observable per layer (#847 acceptance #6). An instrument-creation
// error yields a no-op counter so the Loop always has a usable instrument and
// tests that never wire a real meter provider still work — mirrors
// actions.newWritesCounter.
func newLLMGatedCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(decisionMeterName).Int64Counter(
		"agent_llm_gated_total",
		otelmetric.WithDescription("Total remote decide cycles and retros denied by the LLM call gate, labeled by reason (#847)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Int64Counter("agent_llm_gated_total")
	}
	return c
}

// defaultLLMGatedCounter is the no-op fallback counter a Loop uses until one is
// injected (NewLoop wires the global meter provider; tests may leave it). It keeps
// the gate-increment path nil-safe without every test having to set a counter.
func defaultLLMGatedCounter() otelmetric.Int64Counter {
	return newLLMGatedCounter(otel.GetMeterProvider())
}

// newEvaluationsCounter builds the agent_evaluations_total counter from the given
// meter provider (#1053). Unlike the audit trace — which is emitted ONLY when the
// rules engine returns ≥1 decision — this counter is incremented on EVERY eval
// cycle, so metric arrival is observable even on idle cycles where no rule fires.
// It closes the "are signals even arriving?" blind spot WITHOUT a span per idle
// cycle. Labels are deliberately tiny — signal=metrics|traces and outcome=
// decided|idle (4 series max). NO per-game/per-serial labels: this is a liveness
// pulse, not per-game attribution (that lives on the trace). An instrument-creation
// error yields a no-op counter so the Loop always has a usable instrument and tests
// that never wire a real meter provider still work — mirrors newLLMGatedCounter.
func newEvaluationsCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(decisionMeterName).Int64Counter(
		"agent_evaluations_total",
		otelmetric.WithDescription("Total agent evaluation cycles, labeled by signal (metrics|traces) and outcome (decided|idle); incremented on every cycle including idle ones so signal arrival is observable (#1053)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Int64Counter("agent_evaluations_total")
	}
	return c
}

// defaultEvaluationsCounter is the no-op fallback counter a Loop uses until one is
// injected (NewLoop wires the global meter provider; tests may leave it). It keeps
// the per-cycle increment path nil-safe without every test having to set a counter.
func defaultEvaluationsCounter() otelmetric.Int64Counter {
	return newEvaluationsCounter(otel.GetMeterProvider())
}

// newDecisionsCounter builds the agent_decisions_total counter from the given
// meter provider (#790). It is incremented once per decision the rules/llm engine
// returns, INCLUDING the ones a permission gate blocks, so the demo dashboard's
// applied-vs-blocked panel (#789/#1113) can break decisions down by outcome.
// Labels: action=<intervention id or "noop">, blocked=<bool>, block_reason=<""
// when applied; not_allowed|battery_threshold|already_charged|shield_cap_reached|shield_cooldown|rate_limit when blocked>. The
// block_reason set is the closed set of decision.BlockReason values, so
// cardinality stays bounded. An instrument-creation error yields a no-op counter
// so the Loop always has a usable instrument and tests that never wire a real
// meter provider still work — mirrors newLLMGatedCounter.
func newDecisionsCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(decisionMeterName).Int64Counter(
		"agent_decisions_total",
		otelmetric.WithDescription("Total decisions returned by the agent engine, labeled by action, blocked (bool), and block_reason (empty when applied; not_allowed|battery_threshold|already_charged|shield_cap_reached|shield_cooldown|rate_limit when blocked) (#790)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Int64Counter("agent_decisions_total")
	}
	return c
}

// defaultDecisionsCounter is the no-op fallback counter a Loop uses until one is
// injected (NewLoop wires the global meter provider; tests may leave it). It keeps
// the per-decision increment path nil-safe without every test having to set a counter.
func defaultDecisionsCounter() otelmetric.Int64Counter {
	return newDecisionsCounter(otel.GetMeterProvider())
}

// newInterventionsAppliedCounter builds the agent_interventions_applied_total
// counter from the given meter provider (#790). It is incremented once per
// decision that passes EVERY permission gate AND is successfully applied through
// the ActionSink (a gated no-op while interventions_enabled is off is not
// counted, #1681) — the numerator for the demo dashboard's "interventions applied"
// rate panel (#789/#1113). Label: action=<intervention id or "noop">. An
// instrument-creation error yields a no-op counter so the Loop always has a usable
// instrument and tests that never wire a real meter provider still work — mirrors
// newLLMGatedCounter.
func newInterventionsAppliedCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(decisionMeterName).Int64Counter(
		"agent_interventions_applied_total",
		otelmetric.WithDescription("Total interventions successfully applied through the action sink, labeled by action (#790)"),
	)
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Int64Counter("agent_interventions_applied_total")
	}
	return c
}

// defaultInterventionsAppliedCounter is the no-op fallback counter a Loop uses
// until one is injected (NewLoop wires the global meter provider; tests may leave
// it). It keeps the post-Apply increment path nil-safe without every test having to
// set a counter.
func defaultInterventionsAppliedCounter() otelmetric.Int64Counter {
	return newInterventionsAppliedCounter(otel.GetMeterProvider())
}

// EffectDeltaBuckets are the agent_intervention_effect_delta bucket boundaries,
// symmetric around zero and sized for the 0..1 progress signals (`*_progress`,
// `*_ratio`, `skill_gap`), whose delta lies in -1..1. Without explicit boundaries
// the SDK defaults (0, 5, 10, ...) put every value in one bucket (#1434). Only those
// signals are recorded (effectMetricSignal, #1444): the `*_seconds` and constant
// threshold keys stay on the agent.intervention.effect spans.
var EffectDeltaBuckets = []float64{-1, -0.5, -0.25, -0.1, -0.05, -0.01, 0, 0.01, 0.05, 0.1, 0.25, 0.5, 1}

// newEffectDeltaHistogram builds the agent_intervention_effect_delta histogram from
// the given meter provider (#918). It records the follow_up-baseline change in a
// game's fitness/objective signal a follow-up window after an intervention was
// dispatched, so the game-path effect-measurement loop is queryable downstream (the
// M5 dashboard #791/#792 plots intervention->effect pairs; the #844 retro reads it as
// per-intervention evidence). The value is the change in one fitness signal (unit
// "1"; see EffectDeltaBuckets for the mixed signal units); labels are the
// intervention type, the served objective, and the dotted fitness signal key. An
// instrument-creation error yields a no-op histogram so a Loop always has a usable
// instrument — mirrors newLLMGatedCounter / actions.newWritesCounter.
func newEffectDeltaHistogram(mp otelmetric.MeterProvider) otelmetric.Float64Histogram {
	h, err := mp.Meter(decisionMeterName).Float64Histogram(
		"agent_intervention_effect_delta",
		otelmetric.WithDescription("Change in a game's fitness/objective signal measured a follow-up window after an intervention was dispatched, labeled by intervention type, objective, and signal (#918)"),
		otelmetric.WithUnit("1"),
		// UC-035 BR-004: explicit bucket boundaries, not the SDK defaults.
		otelmetric.WithExplicitBucketBoundaries(EffectDeltaBuckets...),
	)
	if err != nil {
		h, _ = metricnoop.NewMeterProvider().Meter(decisionMeterName).Float64Histogram("agent_intervention_effect_delta")
	}
	return h
}

// defaultEffectDeltaHistogram is the no-op fallback histogram a Loop uses until one is
// injected (NewLoop wires the global meter provider; tests may leave it). It keeps the
// effect-record path nil-safe without every test having to set a histogram.
func defaultEffectDeltaHistogram() otelmetric.Float64Histogram {
	return newEffectDeltaHistogram(otel.GetMeterProvider())
}
