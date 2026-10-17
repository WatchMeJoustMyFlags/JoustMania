package chathttp

import (
	"context"
	"slices"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// UC: UC-035 BR-004
func TestMetrics_ExportSemconvBucketBoundaries(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	previous := otel.GetMeterProvider()
	otel.SetMeterProvider(provider)
	t.Cleanup(func() {
		otel.SetMeterProvider(previous)
		_ = provider.Shutdown(context.Background())
	})

	m := NewMetrics("test/chathttp-buckets")
	call := Call{Provider: "openai", RequestModel: "m", Host: "h", Port: 443}
	m.RecordDuration(context.Background(), call, "", 300*time.Millisecond)
	m.RecordTokens(context.Background(), call, 100, 20)

	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	got := map[string][]float64{}
	for _, sm := range rm.ScopeMetrics {
		for _, md := range sm.Metrics {
			switch data := md.Data.(type) {
			case metricdata.Histogram[float64]:
				got[md.Name] = data.DataPoints[0].Bounds
			case metricdata.Histogram[int64]:
				got[md.Name] = data.DataPoints[0].Bounds
			}
		}
	}
	want := map[string][]float64{
		"gen_ai.client.operation.duration": OperationDurationBuckets,
		"gen_ai.client.token.usage":        TokenUsageBuckets,
	}
	for name, bounds := range want {
		if !slices.Equal(got[name], bounds) {
			t.Errorf("%s bounds = %v, want %v", name, got[name], bounds)
		}
	}
}
