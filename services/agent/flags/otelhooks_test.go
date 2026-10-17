package flags

import (
	"context"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
	"github.com/open-feature/go-sdk/openfeature/memprovider"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// UC: UC-035
func TestMetricVariant(t *testing.T) {
	cases := map[string]string{
		"jev":                    "jev",
		"default":                "default",
		"r7f3a1b2c_17":           "scoped_value",
		"o7f3a1b2c_3":            "overlay_value",
		"r_17":                   "r_17", // no epoch: not a generated name
		"rabc":                   "rabc",
		"route_1":                "route_1", // non-hex epoch
		"o7f3a1b2c_x":            "o7f3a1b2c_x",
		"":                       "",
		"agent_experiment__exp1": "agent_experiment__exp1",
	}
	for in, want := range cases {
		if got := MetricVariant(in); got != want {
			t.Errorf("UC-035/MetricVariant(%q) = %q, want %q", in, got, want)
		}
	}
}

func collectMetrics(t *testing.T, reader *sdkmetric.ManualReader) map[string][]metricdata.DataPoint[int64] {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	out := map[string][]metricdata.DataPoint[int64]{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if sum, ok := m.Data.(metricdata.Sum[int64]); ok {
				out[m.Name] = append(out[m.Name], sum.DataPoints...)
			}
		}
	}
	return out
}

func attr(set attribute.Set, key string) (string, bool) {
	v, ok := set.Value(attribute.Key(key))
	return v.String(), ok
}

// UC: UC-035
// The standard OpenFeature hooks count every evaluation with bounded labels: the flag key,
// provider and a declared variant (a generated flag-service variant becomes its class), and the
// error counter without the free-text error.message (dropped by MetricViews).
func TestOTelHooks_BoundedMetricLabelsAndSpanEvent(t *testing.T) {
	const domain = "otelhooks-test"
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader), sdkmetric.WithView(MetricViews()...))
	hooks, err := newOTelHooks(mp)
	if err != nil {
		t.Fatal(err)
	}

	provider := memprovider.NewInMemoryProvider(map[string]memprovider.InMemoryFlag{
		"decision.backend": {
			State: memprovider.Enabled, DefaultVariant: "jev",
			Variants: map[string]any{"jev": "jev", "rules": "rules"},
		},
		"shield_seconds": {
			State: memprovider.Enabled, DefaultVariant: "r7f3a1b2c_17",
			Variants: map[string]any{"none": 0.0, "r7f3a1b2c_17": 5.0},
		},
	})
	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = openfeature.SetNamedProviderAndWait(domain, openfeature.NoopProvider{}) })
	client := openfeature.NewClient(domain)
	client.AddHooks(hooks...)

	recorder := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(recorder))
	ctx, span := tp.Tracer("test").Start(context.Background(), "agent.decide")
	evalCtx := openfeature.NewEvaluationContext("game_abc123def456", nil)

	if v, _ := client.StringValue(ctx, "decision.backend", "rules", evalCtx); v != "jev" {
		t.Fatalf("decision.backend = %q, want jev", v)
	}
	if v, _ := client.FloatValue(ctx, "shield_seconds", 0, evalCtx); v != 5.0 {
		t.Fatalf("shield_seconds = %v, want 5 (the hook must not change the result)", v)
	}
	if _, err := client.StringValue(ctx, "no.such.flag", "x", evalCtx); err == nil {
		t.Fatal("missing flag evaluated without error")
	}
	span.End()

	got := collectMetrics(t, reader)
	for _, name := range []string{
		"feature_flag.evaluation_requests_total", "feature_flag.evaluation_success_total",
		"feature_flag.evaluation_error_total", "feature_flag.evaluation_active_count",
	} {
		if len(got[name]) == 0 {
			t.Errorf("UC-035: metric %s not recorded", name)
		}
	}

	variants := map[string]string{}
	for _, dp := range got["feature_flag.evaluation_success_total"] {
		key, _ := attr(dp.Attributes, "feature_flag.key")
		variant, _ := attr(dp.Attributes, "feature_flag.result.variant")
		variants[key] = variant
		if _, ok := attr(dp.Attributes, "feature_flag.context.id"); ok {
			t.Errorf("UC-035: targeting key on a metric label: %v", dp.Attributes)
		}
	}
	if variants["decision.backend"] != "jev" || variants["shield_seconds"] != "scoped_value" {
		t.Errorf("UC-035: success variants = %v, want decision.backend=jev, shield_seconds=scoped_value", variants)
	}
	for _, dp := range got["feature_flag.evaluation_error_total"] {
		if _, ok := attr(dp.Attributes, ErrorMessageAttr); ok {
			t.Errorf("UC-035: %s on the error counter: %v", ErrorMessageAttr, dp.Attributes)
		}
	}

	// The traces hook: one feature_flag.evaluation event per evaluation, real variant kept.
	ended := recorder.Ended()
	if len(ended) != 1 {
		t.Fatalf("spans = %d, want 1", len(ended))
	}
	events := 0
	for _, ev := range ended[0].Events() {
		if ev.Name != "feature_flag.evaluation" {
			continue
		}
		events++
		set := attribute.NewSet(ev.Attributes...)
		if key, _ := attr(set, "feature_flag.key"); key == "shield_seconds" {
			if v, _ := attr(set, "feature_flag.result.variant"); v != "r7f3a1b2c_17" {
				t.Errorf("UC-035: span event variant = %q, want the real name", v)
			}
		}
	}
	if events != 3 {
		t.Errorf("UC-035: feature_flag.evaluation events = %d, want 3", events)
	}
}
