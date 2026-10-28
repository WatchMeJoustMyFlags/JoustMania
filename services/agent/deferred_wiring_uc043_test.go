package main

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/promote"
)

func gameActiveKindMetric(gameID, kind string, value float64) pmetric.Metrics {
	md := gameActiveMetric(gameID)
	dp := md.ResourceMetrics().At(0).ScopeMetrics().At(0).Metrics().At(0).Gauge().DataPoints().At(0)
	dp.SetDoubleValue(value)
	if kind != "" {
		dp.Attributes().PutStr("game_kind", kind)
	}
	return md
}

// UC: UC-043 A17
func TestRealGameLive_Probe(t *testing.T) {
	clock := time.Unix(20_000, 0)
	now := func() time.Time { return clock }
	newMux := func() *gamecontext.Multiplexer {
		return gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
			return gamecontext.NewStore(time.Hour, time.Hour, now)
		})
	}
	cases := []struct {
		name  string
		kind  string
		value float64
		age   time.Duration
		want  bool
	}{
		{"an active fresh real game is live", "primary", 1, 0, true},
		{"an active game without a kind is live (fail closed)", "", 1, 0, true},
		{"a shadow game is not live", "shadow", 1, 0, false},
		{"a stale real partition is not live", "primary", 1, liveStaleness + time.Second, false},
		{"an ended real game is not live", "primary", 0, 0, false},
	}
	for _, tc := range cases {
		t.Run("UC-043/A17 "+tc.name, func(t *testing.T) {
			clock = time.Unix(20_000, 0)
			mux := newMux()
			mux.ApplyMetrics(gameActiveKindMetric("game_1", tc.kind, tc.value))
			clock = clock.Add(tc.age)
			live, err := realGameLiveAt(func() *gamecontext.Multiplexer { return mux }, now)(context.Background())
			if err != nil || live != tc.want {
				t.Fatalf("live = %v err %v, want %v", live, err, tc.want)
			}
		})
	}
	t.Run("UC-043/A17 no multiplexer yet errors (the promoter treats it as live)", func(t *testing.T) {
		live, err := realGameLiveAt(func() *gamecontext.Multiplexer { return nil }, now)(context.Background())
		if err == nil || !live {
			t.Fatalf("live = %v err %v", live, err)
		}
	})
}

// UC: UC-043 A17
func TestWireDeferredPromotions(t *testing.T) {
	t.Run("UC-043/A17 main wires the real-game probe and both deferred gauges", func(t *testing.T) {
		p := promote.NewPromoter(nil, nil, nil, nil, nil, promote.RepoRef{}, nil, nil)
		reader := sdkmetric.NewManualReader()
		mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
		wireDeferredPromotions(p, func() *gamecontext.Multiplexer { return nil }, mp, nil)
		if !p.HasRealGameProbe() {
			t.Fatal("no real-game probe wired: every hold-key promotion would wait forever")
		}
		var rm metricdata.ResourceMetrics
		if err := reader.Collect(context.Background(), &rm); err != nil {
			t.Fatal(err)
		}
		seen := map[string]bool{}
		for _, sm := range rm.ScopeMetrics {
			for _, m := range sm.Metrics {
				seen[m.Name] = true
			}
		}
		for _, name := range []string{promote.MetricDeferredPending, promote.MetricDeferredOldest} {
			if !seen[name] {
				t.Fatalf("gauge %s not exported; got %v", name, seen)
			}
		}
	})
}
