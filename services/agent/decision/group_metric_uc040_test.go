package decision

import (
	"context"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
)

// group_metric_uc040_test.go: agent_group_interventions_total{outcome,reason,backend,
// game_kind,shape} (#2467 PR 6; UC-040 A47, A48, BR-033; telemetry.md section 4).

func groupMetricByLabels(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_group_interventions_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				v := func(k string) string { a, _ := dp.Attributes.Value(attribute.Key(k)); return a.AsString() }
				out[v("outcome")+"/"+v("reason")+"/"+v("backend")+"/"+v("game_kind")+"/"+v("shape")] = dp.Value
			}
		}
	}
	return out
}

// UC: UC-040 A47 BR-033
func TestGroupInterventionsCounter(t *testing.T) {
	ctx := context.Background()
	run := func(t *testing.T, mutate func(l *Loop)) map[string]int64 {
		reader := metric.NewManualReader()
		l, _, clock, _ := budgetLoop(t, 100)
		l.groupInterventions = newGroupInterventionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		mutate(l)
		var st LayerState
		l.runDecision(ctx, groupSnapshot(groupPolicy()), groupCtx("shadow", "A", "B", "C"), groupDecision("A", "B", "C"), *clock, &st)
		return groupMetricByLabels(t, reader)
	}
	t.Run("UC-040/A47 an applied group of every alive player counts shape=all", func(t *testing.T) {
		got := run(t, func(*Loop) {})
		if want := "applied//rules/shadow/all"; got[want] != 1 || len(got) != 1 {
			t.Fatalf("counts = %v, want %s once", got, want)
		}
	})
	t.Run("UC-040/A47 a group that leaves one alive player out counts shape=all_except", func(t *testing.T) {
		reader := metric.NewManualReader()
		l, _, clock, _ := budgetLoop(t, 100)
		l.groupInterventions = newGroupInterventionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		var st LayerState
		l.runDecision(ctx, groupSnapshot(groupPolicy()), groupCtx("shadow", "A", "B", "C"), groupDecision("A", "B"), *clock, &st)
		if got := groupMetricByLabels(t, reader); got["applied//rules/shadow/all_except"] != 1 {
			t.Fatalf("counts = %v", got)
		}
	})
	t.Run("UC-040/A48 a refused group counts outcome=blocked with the block reason", func(t *testing.T) {
		reader := metric.NewManualReader()
		l, _, clock, _ := budgetLoop(t, 100)
		l.groupInterventions = newGroupInterventionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		p := groupPolicy()
		p.MaxInterventionsPerMinute = 0
		var st LayerState
		l.runDecision(ctx, groupSnapshot(p), groupCtx("shadow", "A", "B", "C"), groupDecision("A", "B", "C"), *clock, &st)
		if got := groupMetricByLabels(t, reader); got["blocked/rate_limit/rules/shadow/all"] != 1 {
			t.Fatalf("counts = %v", got)
		}
	})
	t.Run("UC-040/A27 a failed write counts outcome=failed", func(t *testing.T) {
		got := run(t, func(l *Loop) { l.Actions = errSink{} })
		if got["failed/write_failed/rules/shadow/all"] != 1 {
			t.Fatalf("counts = %v", got)
		}
	})
	t.Run("UC-040/A47 a single grant adds no group sample", func(t *testing.T) {
		reader := metric.NewManualReader()
		l, _, clock, _ := budgetLoop(t, 100)
		l.groupInterventions = newGroupInterventionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		var st LayerState
		d := groupDecision()
		d.Intervention, d.TargetSerial = InterventionGrantShield, "A"
		l.runDecision(ctx, groupSnapshot(groupPolicy()), groupCtx("shadow", "A", "B", "C"), d, *clock, &st)
		if got := groupMetricByLabels(t, reader); len(got) != 0 {
			t.Fatalf("counts = %v, want none", got)
		}
	})
	t.Run("UC-040/A47 the decision span carries the alive count at the group", func(t *testing.T) {
		snap := groupSnapshot(groupPolicy())
		l, sr := decideRecordingLoop(t, snap)
		l.Rules = fakeRules{out: []Decision{groupDecision("A", "B")}}
		l.Actions = &capturingSink{}
		var st LayerState
		l.runDecision(ctx, snap, groupCtx("shadow", "A", "B", "C"), groupDecision("A", "B"), time.Unix(50_000, 0), &st)
		spans := spansByName(sr.Ended(), SpanDecision)
		if len(spans) != 1 {
			t.Fatalf("%d decision spans, want 1", len(spans))
		}
		for _, kv := range spans[0].Attributes() {
			if kv.Key == AttrDecisionGroupAlive {
				if kv.Value.AsInt64() != 3 {
					t.Fatalf("%s = %d, want 3", kv.Key, kv.Value.AsInt64())
				}
				return
			}
		}
		t.Fatalf("no %s attribute", AttrDecisionGroupAlive)
	})
}
