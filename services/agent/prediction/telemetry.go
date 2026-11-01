package prediction

import (
	"context"
	"strconv"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/goal"
	"github.com/joustmania/agent/telemetry/keys"
)

// telemetry.go emits agent.prediction.outcome and the agent_prediction* metrics
// (docs/specs/telemetry.md §4.1, §6). Every metric carries game_kind (primary /
// shadow, gamecontext.TelemetryGameKind), and the outcome and Brier metrics carry
// acted, so counterfactuals never mix into the acted calibration.

const meterName = "github.com/joustmania/agent/prediction"

// Metric names.
const (
	MetricRegistered = "agent_predictions_registered_total"
	MetricOutcomes   = "agent_prediction_outcomes_total"
	MetricBrierSum   = "agent_prediction_brier_sum"
	MetricMerged     = "agent_prediction_merged_total"
	// MetricGoalAttainment is the per-game goal attainment 0..1 (#2479, §14.4).
	MetricGoalAttainment = "agent_goal_attainment"
)

type telemetry struct {
	tracer      trace.Tracer
	registeredC otelmetric.Int64Counter
	outcomes    otelmetric.Int64Counter
	brier       otelmetric.Float64Counter
	mergedC     otelmetric.Int64Counter
	goalH       otelmetric.Float64Histogram
}

func defaultTelemetry() telemetry {
	return newTelemetry(otel.GetTracerProvider(), otel.GetMeterProvider())
}

func newTelemetry(tp trace.TracerProvider, mp otelmetric.MeterProvider) telemetry {
	m := mp.Meter(meterName)
	noop := metricnoop.NewMeterProvider().Meter(meterName)
	t := telemetry{tracer: tp.Tracer(meterName)}
	var err error
	if t.registeredC, err = m.Int64Counter(MetricRegistered,
		otelmetric.WithDescription("Decision predictions and base-rate controls registered (#2479)")); err != nil {
		t.registeredC, _ = noop.Int64Counter(MetricRegistered)
	}
	if t.outcomes, err = m.Int64Counter(MetricOutcomes,
		otelmetric.WithDescription("Scored predictions by outcome hit / miss / unscorable (#2479)")); err != nil {
		t.outcomes, _ = noop.Int64Counter(MetricOutcomes)
	}
	if t.brier, err = m.Float64Counter(MetricBrierSum,
		otelmetric.WithDescription("Sum of (p - y)^2 over hit and miss predictions; divide by the hit+miss count for the Brier score (#2479)")); err != nil {
		t.brier, _ = noop.Float64Counter(MetricBrierSum)
	}
	if t.mergedC, err = m.Int64Counter(MetricMerged,
		otelmetric.WithDescription("Predictions not registered because an identical one was pending (#2479)")); err != nil {
		t.mergedC, _ = noop.Int64Counter(MetricMerged)
	}
	if t.goalH, err = m.Float64Histogram(MetricGoalAttainment,
		otelmetric.WithDescription("Goal attainment 0..1 per ended game: the weighted mean of the goal profile's term scores (#2479)"),
		otelmetric.WithExplicitBucketBoundaries(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1)); err != nil {
		t.goalH, _ = noop.Float64Histogram(MetricGoalAttainment)
	}
	return t
}

// SetTelemetry overrides the tracer and meter providers (tests, main wiring).
// Set before use.
func (b *Book) SetTelemetry(tp trace.TracerProvider, mp otelmetric.MeterProvider) {
	b.tel = newTelemetry(tp, mp)
}

func baseAttrs(backend string, p decide.Prediction, gameKind string) []attribute.KeyValue {
	return []attribute.KeyValue{
		attribute.String("backend", backend),
		attribute.String("source", p.Source),
		attribute.String("template", p.Template),
		attribute.String("game_kind", gamecontext.TelemetryGameKind(gameKind)),
	}
}

// goalAttainment records agent_goal_attainment{profile,game_kind} for a game whose
// goal had at least one measured term.
func (t telemetry) goalAttainment(r goal.Result, gameKind string) {
	if !r.HasAttainment {
		return
	}
	t.goalH.Record(context.Background(), r.Attainment, otelmetric.WithAttributes(
		attribute.String("profile", r.Profile.Name),
		attribute.String("game_kind", gamecontext.TelemetryGameKind(gameKind))))
}

func (t telemetry) registered(backend string, p decide.Prediction, gameKind string) {
	t.registeredC.Add(context.Background(), 1, otelmetric.WithAttributes(baseAttrs(backend, p, gameKind)...))
}

func (t telemetry) merged(backend string, p decide.Prediction, gameKind string) {
	t.mergedC.Add(context.Background(), 1, otelmetric.WithAttributes(baseAttrs(backend, p, gameKind)...))
}

// outcome records r's metrics and its agent.prediction.outcome span.
func (t telemetry) outcome(ctx context.Context, r Result) {
	if ctx.Err() != nil {
		ctx = context.Background()
	}
	acted := attribute.String("acted", strconv.FormatBool(r.Acted))
	attrs := append(baseAttrs(r.Backend, r.Prediction, r.GameKind), acted)
	t.outcomes.Add(ctx, 1, otelmetric.WithAttributes(append(attrs, attribute.String("outcome", r.Outcome))...))
	if r.Outcome != OutcomeUnscorable {
		y := 0.0
		if r.Outcome == OutcomeHit {
			y = 1
		}
		d := r.Prediction.P - y
		t.brier.Add(ctx, d*d, otelmetric.WithAttributes(attrs...))
	}

	parent := context.Background()
	if p, ok := remoteParent(r.parent); ok {
		parent = trace.ContextWithRemoteSpanContext(parent, p)
	}
	opts := []trace.SpanStartOption{
		trace.WithTimestamp(r.RegisteredAt),
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(spanAttrs(r)...),
	}
	if r.decideSC.IsValid() {
		opts = append(opts, trace.WithLinks(trace.Link{
			SpanContext: r.decideSC,
			Attributes:  []attribute.KeyValue{attribute.String("link.kind", keys.LinkKindPrediction)},
		}))
	}
	_, span := t.tracer.Start(parent, keys.SpanPredictionOutcome, opts...)
	span.End(trace.WithTimestamp(r.ScoredAt))
}

func spanAttrs(r Result) []attribute.KeyValue {
	p := r.Prediction
	a := []attribute.KeyValue{
		attribute.String(keys.AttrGameID, r.GameID),
		attribute.String(keys.AttrSessionID, r.GameID),
		attribute.String(keys.AttrGameKind, r.GameKind),
		attribute.String(keys.AttrPredictionID, r.ID),
		attribute.String(keys.AttrPredictionTemplate, p.Template),
		attribute.String(keys.AttrPredictionCharacteristic, p.Characteristic),
		attribute.String(keys.AttrPredictionScope, p.Scope),
		attribute.Int(keys.AttrPredictionHorizonS, p.HorizonS),
		attribute.Float64(keys.AttrPredictionP, p.P),
		attribute.String(keys.AttrPredictionSource, p.Source),
		attribute.String(keys.AttrPredictionBackend, r.Backend),
		attribute.Bool(keys.AttrPredictionActed, r.Acted),
		attribute.String(keys.AttrPredictionOutcome, r.Outcome),
		attribute.Bool(keys.AttrPredictionDecidedEarly, r.DecidedEarly),
		attribute.Float64(keys.AttrPredictionScoredAtOffsetS, r.ScoredAt.Sub(r.RegisteredAt).Seconds()),
	}
	if p.Target != "" {
		a = append(a, attribute.String(keys.AttrPredictionTarget, p.Target))
	}
	if p.Rule != "" {
		a = append(a, attribute.String(keys.AttrPredictionRule, p.Rule))
	}
	if r.UnscorableReason != "" {
		a = append(a, attribute.String(keys.AttrPredictionUnscorableReason, r.UnscorableReason))
	}
	if r.Before != nil {
		a = append(a, attribute.Float64(keys.AttrPredictionBefore, *r.Before))
	}
	if r.After != nil {
		a = append(a, attribute.Float64(keys.AttrPredictionAfter, *r.After))
	}
	if r.Eliminations != nil {
		a = append(a, attribute.Int(keys.AttrPredictionEliminations, *r.Eliminations))
	}
	if r.EndOffsetS != nil {
		a = append(a, attribute.Float64(keys.AttrPredictionEndOffsetS, *r.EndOffsetS))
	}
	if r.Approx {
		a = append(a, attribute.String(keys.AttrPredictionTiming, "approx"))
	}
	return a
}

// remoteParent is the game's game_cycle, else gameplay_phase, else root span as a
// remote span context (the order of decision.inGameRemoteParent, UC-040 BR-013).
func remoteParent(g gameParent) (trace.SpanContext, bool) {
	tid, err := trace.TraceIDFromHex(g.traceID)
	if err != nil || !tid.IsValid() {
		return trace.SpanContext{}, false
	}
	for _, s := range []string{g.cycleSpanID, g.phaseSpanID, g.rootSpanID} {
		sid, err := trace.SpanIDFromHex(s)
		if err != nil || !sid.IsValid() {
			continue
		}
		return trace.NewSpanContext(trace.SpanContextConfig{
			TraceID: tid, SpanID: sid, TraceFlags: trace.FlagsSampled, Remote: true,
		}), true
	}
	return trace.SpanContext{}, false
}
