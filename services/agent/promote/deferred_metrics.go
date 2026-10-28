package promote

import (
	"context"

	otelmetric "go.opentelemetry.io/otel/metric"
)

// deferred_metrics.go: the gauges of the deferred queue (UC-043 A17 step 3,
// telemetry.md §6, opposer M4): a queue that never drains (no probe, a guard that
// keeps refusing, back-to-back real games) must be visible.

const (
	deferredMeterName = "github.com/joustmania/agent/promote"
	// MetricDeferredPending counts the waiting writes and reverts.
	MetricDeferredPending = "agent_promotion_deferred_pending"
	// MetricDeferredOldest is the age of the oldest waiting entry in seconds.
	MetricDeferredOldest = "agent_promotion_deferred_oldest_seconds"
)

// RegisterDeferredMetrics registers the two observable gauges on mp.
func (pr *Promoter) RegisterDeferredMetrics(mp otelmetric.MeterProvider) error {
	meter := mp.Meter(deferredMeterName)
	pending, err := meter.Int64ObservableGauge(MetricDeferredPending,
		otelmetric.WithDescription("Autonomous real writes and agent reverts of hold keys waiting until no real game runs (UC-043 A17)"))
	if err != nil {
		return err
	}
	oldest, err := meter.Float64ObservableGauge(MetricDeferredOldest,
		otelmetric.WithDescription("Age in seconds of the oldest waiting deferred promotion entry, 0 when none (UC-043 A17)"))
	if err != nil {
		return err
	}
	_, err = meter.RegisterCallback(func(_ context.Context, o otelmetric.Observer) error {
		n, age := pr.DeferredStats()
		o.ObserveInt64(pending, int64(n))
		o.ObserveFloat64(oldest, age.Seconds())
		return nil
	}, pending, oldest)
	return err
}
