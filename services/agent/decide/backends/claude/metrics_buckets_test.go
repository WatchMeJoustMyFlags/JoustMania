package claude

import (
	"context"
	"slices"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decide/backends/internal/chathttp"
)

// UC: UC-035 BR-004
func TestClaudeMetrics_ExportSemconvBucketBoundaries(t *testing.T) {
	reader := sdkmetric.NewManualReader()
	provider := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	previous := otel.GetMeterProvider()
	otel.SetMeterProvider(provider)
	// The claude instruments are package-level and created once; rebuild them
	// against this test's provider, and again afterwards against the restored one.
	metricsOnce = sync.Once{}
	t.Cleanup(func() {
		otel.SetMeterProvider(previous)
		metricsOnce = sync.Once{}
		_ = provider.Shutdown(context.Background())
	})

	b := &Backend{}
	recordDuration(context.Background(), b, "m", "", 300*time.Millisecond)
	recordTokens(context.Background(), b, "m", 100, 20)

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
		"gen_ai.client.operation.duration": chathttp.OperationDurationBuckets,
		"gen_ai.client.token.usage":        chathttp.TokenUsageBuckets,
	}
	for name, bounds := range want {
		if !slices.Equal(got[name], bounds) {
			t.Errorf("%s bounds = %v, want %v", name, got[name], bounds)
		}
	}
}
