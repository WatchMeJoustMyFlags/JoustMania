package decision

import (
	"context"
	"slices"
	"testing"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// UC: UC-035 BR-004
func TestEffectDeltaHistogram_ExportsExplicitBuckets(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	t.Cleanup(func() { _ = provider.Shutdown(context.Background()) })

	newEffectDeltaHistogram(provider).Record(context.Background(), 0.07)

	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, md := range sm.Metrics {
			if md.Name != "agent_intervention_effect_delta" {
				continue
			}
			bounds := md.Data.(metricdata.Histogram[float64]).DataPoints[0].Bounds
			if !slices.Equal(bounds, EffectDeltaBuckets) {
				t.Fatalf("bounds = %v, want %v", bounds, EffectDeltaBuckets)
			}
			return
		}
	}
	t.Fatal("agent_intervention_effect_delta not exported")
}
