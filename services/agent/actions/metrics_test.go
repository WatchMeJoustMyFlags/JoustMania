package actions

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/decision"
)

// newMeteredWriter builds a Writer whose agent_intervention_writes_total counter
// is wired to a manual-reader meter provider, returning the Writer, its file
// path, and the reader for collecting metrics.
func newMeteredWriter(t *testing.T) (*Writer, string, *metric.ManualReader) {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("testdata", "interventions.json"))
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "interventions.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatalf("write temp fixture: %v", err)
	}

	reader := metric.NewManualReader()
	mp := metric.NewMeterProvider(metric.WithReader(reader))

	w := &Writer{
		path:       path,
		log:        slog.New(slog.NewTextHandler(os.Stderr, nil)),
		nonce:      newNonceGen(),
		writes:     newWritesCounter(mp),
		corruption: newCorruptionCounter(mp),
	}
	return w, path, reader
}

// corruptionByPhase collects the manual reader and returns a phase->value map for
// the agent_intervention_file_corruption_total counter (empty when not yet
// recorded).
func corruptionByPhase(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_intervention_file_corruption_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				phase, _ := dp.Attributes.Value("phase")
				out[phase.AsString()] = dp.Value
			}
		}
	}
	return out
}

// writesByResult collects the manual reader and returns a result->value map for
// the agent_intervention_writes_total counter (empty when not yet recorded).
func writesByResult(t *testing.T, reader *metric.ManualReader) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	out := map[string]int64{}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "agent_intervention_writes_total" {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("expected Sum[int64], got %T", m.Data)
			}
			for _, dp := range sum.DataPoints {
				result, _ := dp.Attributes.Value("result")
				out[result.AsString()] = dp.Value
			}
		}
	}
	return out
}

func TestApply_RecordsOKResult(t *testing.T) {
	w, _, reader := newMeteredWriter(t)

	err := w.Apply(context.Background(), decision.Decision{
		Intervention: decision.InterventionEndGame,
	})
	if err != nil {
		t.Fatalf("Apply returned error: %v", err)
	}

	got := writesByResult(t, reader)
	if got["ok"] != 1 {
		t.Errorf("result=ok count = %d, want 1", got["ok"])
	}
	if got["error"] != 0 {
		t.Errorf("result=error count = %d, want 0", got["error"])
	}
}

func TestApply_RecordsErrorResult_OnMissingFile(t *testing.T) {
	w, path, reader := newMeteredWriter(t)
	// Remove the backing file so the read-modify-write cycle fails.
	if err := os.Remove(path); err != nil {
		t.Fatalf("remove fixture: %v", err)
	}

	err := w.Apply(context.Background(), decision.Decision{
		Intervention: decision.InterventionEndGame,
	})
	if err == nil {
		t.Fatal("Apply succeeded, want error from missing file")
	}

	got := writesByResult(t, reader)
	if got["error"] != 1 {
		t.Errorf("result=error count = %d, want 1", got["error"])
	}
	if got["ok"] != 0 {
		t.Errorf("result=ok count = %d, want 0", got["ok"])
	}
}

func TestApply_RecordsErrorResult_OnUnknownIntervention(t *testing.T) {
	w, _, reader := newMeteredWriter(t)

	err := w.Apply(context.Background(), decision.Decision{
		Intervention: "definitely_not_a_real_intervention",
	})
	if err == nil {
		t.Fatal("Apply succeeded, want error from unknown intervention")
	}

	got := writesByResult(t, reader)
	if got["error"] != 1 {
		t.Errorf("result=error count = %d, want 1", got["error"])
	}
}
