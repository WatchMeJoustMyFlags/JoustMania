// Copyright 2026 JoustMania Authors
// SPDX-License-Identifier: Apache-2.0

package dtbizeventsexporter

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/collector/component/componenttest"
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.uber.org/zap"
	"go.uber.org/zap/zaptest"
)

// newTestMetrics creates a pmetric.Metrics with the given configuration.
func newTestMetrics(serviceName, metricName string, value float64, serial string, ts time.Time) pmetric.Metrics {
	md := pmetric.NewMetrics()
	rm := md.ResourceMetrics().AppendEmpty()
	rm.Resource().Attributes().PutStr("service.name", serviceName)

	sm := rm.ScopeMetrics().AppendEmpty()
	m := sm.Metrics().AppendEmpty()
	m.SetName(metricName)
	g := m.SetEmptyGauge()
	dp := g.DataPoints().AppendEmpty()
	dp.SetDoubleValue(value)
	if serial != "" {
		dp.Attributes().PutStr("serial", serial)
	}
	dp.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	return md
}

func TestConsumeMetrics_GaugeDataPoints(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)
	md := newTestMetrics("controller-manager", "acceleration", 1.5, "abc123", ts)

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	if len(exp.buffer) != 1 {
		t.Fatalf("expected 1 event in buffer, got %d", len(exp.buffer))
	}

	ev := exp.buffer[0]
	if ev.EventType != "com.joustmania.acceleration" {
		t.Errorf("event.type = %q, want %q", ev.EventType, "com.joustmania.acceleration")
	}
	if ev.EventSource != "controller-manager" {
		t.Errorf("event.source = %q, want %q", ev.EventSource, "controller-manager")
	}
	if ev.MetricName != "acceleration" {
		t.Errorf("metric.name = %q, want %q", ev.MetricName, "acceleration")
	}
	if ev.MetricValue != 1.5 {
		t.Errorf("metric.value = %f, want %f", ev.MetricValue, 1.5)
	}
	if ev.Serial != "abc123" {
		t.Errorf("serial = %q, want %q", ev.Serial, "abc123")
	}
}

func TestConsumeMetrics_MultipleDataPoints(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)

	md := pmetric.NewMetrics()

	// First resource
	rm1 := md.ResourceMetrics().AppendEmpty()
	rm1.Resource().Attributes().PutStr("service.name", "svc-a")
	sm1 := rm1.ScopeMetrics().AppendEmpty()
	m1 := sm1.Metrics().AppendEmpty()
	m1.SetName("metric_a")
	g1 := m1.SetEmptyGauge()
	dp1 := g1.DataPoints().AppendEmpty()
	dp1.SetDoubleValue(1.0)
	dp1.Attributes().PutStr("serial", "s1")
	dp1.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	// Second resource with a different scope
	rm2 := md.ResourceMetrics().AppendEmpty()
	rm2.Resource().Attributes().PutStr("service.name", "svc-b")
	sm2 := rm2.ScopeMetrics().AppendEmpty()
	m2 := sm2.Metrics().AppendEmpty()
	m2.SetName("metric_b")
	g2 := m2.SetEmptyGauge()
	dp2 := g2.DataPoints().AppendEmpty()
	dp2.SetDoubleValue(2.0)
	dp2.Attributes().PutStr("serial", "s2")
	dp2.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	if len(exp.buffer) != 2 {
		t.Fatalf("expected 2 events, got %d", len(exp.buffer))
	}

	if exp.buffer[0].EventSource != "svc-a" {
		t.Errorf("first event source = %q, want %q", exp.buffer[0].EventSource, "svc-a")
	}
	if exp.buffer[1].EventSource != "svc-b" {
		t.Errorf("second event source = %q, want %q", exp.buffer[1].EventSource, "svc-b")
	}
}

func TestConsumeMetrics_MissingSerial(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)

	md := pmetric.NewMetrics()
	rm := md.ResourceMetrics().AppendEmpty()
	rm.Resource().Attributes().PutStr("service.name", "test-svc")
	sm := rm.ScopeMetrics().AppendEmpty()
	m := sm.Metrics().AppendEmpty()
	m.SetName("accel")
	g := m.SetEmptyGauge()
	dp := g.DataPoints().AppendEmpty()
	dp.SetDoubleValue(3.14)
	// No serial attribute set.
	dp.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	if len(exp.buffer) != 1 {
		t.Fatalf("expected 1 event, got %d", len(exp.buffer))
	}
	if exp.buffer[0].Serial != "" {
		t.Errorf("serial = %q, want empty string", exp.buffer[0].Serial)
	}
}

func TestConsumeMetrics_ServiceNameFallback(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)

	md := pmetric.NewMetrics()
	rm := md.ResourceMetrics().AppendEmpty()
	// No service.name attribute set on the resource.
	sm := rm.ScopeMetrics().AppendEmpty()
	m := sm.Metrics().AppendEmpty()
	m.SetName("test_metric")
	g := m.SetEmptyGauge()
	dp := g.DataPoints().AppendEmpty()
	dp.SetDoubleValue(42.0)
	dp.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	if len(exp.buffer) != 1 {
		t.Fatalf("expected 1 event, got %d", len(exp.buffer))
	}
	if exp.buffer[0].EventSource != "unknown" {
		t.Errorf("event.source = %q, want %q", exp.buffer[0].EventSource, "unknown")
	}
}

func TestConsumeMetrics_TimestampConversion(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 3, 13, 10, 30, 45, 123456789, time.UTC)
	md := newTestMetrics("test-svc", "m", 1.0, "s", ts)

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	expected := ts.Format(time.RFC3339Nano)
	if exp.buffer[0].Timestamp != expected {
		t.Errorf("timestamp = %q, want %q", exp.buffer[0].Timestamp, expected)
	}
}

func TestSendBatch_VerifyRequest(t *testing.T) {
	var mu sync.Mutex
	var receivedBody []byte
	var receivedHeaders http.Header

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		receivedHeaders = r.Header.Clone()
		receivedBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	logger := zaptest.NewLogger(t)
	cfg := &Config{
		Endpoint:        server.URL,
		APIToken:        "test-token-123",
		BatchIntervalMS: 1000,
	}
	exp := newExporter(cfg, logger)
	exp.client = &http.Client{Timeout: 5 * time.Second}

	events := []Bizevent{
		{
			EventType:   "com.joustmania.acceleration",
			EventSource: "controller-manager",
			MetricName:  "acceleration",
			MetricValue: 1.5,
			Serial:      "abc",
			Timestamp:   "2026-01-15T12:00:00Z",
		},
	}

	exp.sendBatch(events)

	mu.Lock()
	defer mu.Unlock()

	if receivedHeaders.Get("Content-Type") != "application/json" {
		t.Errorf("Content-Type = %q, want %q", receivedHeaders.Get("Content-Type"), "application/json")
	}
	if receivedHeaders.Get("Authorization") != "Api-Token test-token-123" {
		t.Errorf("Authorization = %q, want %q", receivedHeaders.Get("Authorization"), "Api-Token test-token-123")
	}

	var got []Bizevent
	if err := json.Unmarshal(receivedBody, &got); err != nil {
		t.Fatalf("failed to unmarshal request body: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 event in body, got %d", len(got))
	}
	if got[0].MetricValue != 1.5 {
		t.Errorf("metric.value = %f, want %f", got[0].MetricValue, 1.5)
	}
}

func TestShutdown_FlushesBuffer(t *testing.T) {
	var mu sync.Mutex
	var receivedBody []byte

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		receivedBody, _ = io.ReadAll(r.Body)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	logger := zaptest.NewLogger(t)
	cfg := &Config{
		Endpoint:        server.URL,
		APIToken:        "tok",
		BatchIntervalMS: 60000, // Long interval so batch loop won't fire.
	}
	exp := newExporter(cfg, logger)
	exp.Start(context.Background(), componenttest.NewNopHost())

	ts := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	md := newTestMetrics("svc", "m", 9.9, "serial1", ts)
	exp.ConsumeMetrics(context.Background(), md)

	exp.Shutdown(context.Background())

	mu.Lock()
	defer mu.Unlock()

	if receivedBody == nil {
		t.Fatal("expected buffered events to be flushed on shutdown, but no request received")
	}

	var got []Bizevent
	if err := json.Unmarshal(receivedBody, &got); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected 1 event flushed on shutdown, got %d", len(got))
	}
	if got[0].MetricValue != 9.9 {
		t.Errorf("metric.value = %f, want %f", got[0].MetricValue, 9.9)
	}
}

func TestDryRunMode_NoHTTPCall(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{
		Endpoint:        "", // Empty = dry run.
		BatchIntervalMS: 1000,
	}
	exp := newExporter(cfg, logger)
	exp.client = &http.Client{Timeout: 5 * time.Second}

	events := []Bizevent{
		{
			EventType:   "com.joustmania.acceleration",
			EventSource: "svc",
			MetricName:  "m",
			MetricValue: 1.0,
			Serial:      "s",
			Timestamp:   "2026-01-01T00:00:00Z",
		},
	}

	// Should not panic or make any HTTP call.
	exp.sendBatch(events)
}

func TestHTTPErrorHandling(t *testing.T) {
	for _, code := range []int{400, 403, 500, 503} {
		t.Run(http.StatusText(code), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.WriteHeader(code)
			}))
			defer server.Close()

			logger := zaptest.NewLogger(t)
			cfg := &Config{
				Endpoint:        server.URL,
				APIToken:        "tok",
				BatchIntervalMS: 1000,
			}
			exp := newExporter(cfg, logger)
			exp.client = &http.Client{Timeout: 5 * time.Second}

			events := []Bizevent{{EventType: "test", MetricValue: 1.0}}

			// Should not panic and should not return any error.
			exp.sendBatch(events)
		})
	}
}

func TestBizeventJSONSerialization(t *testing.T) {
	ev := Bizevent{
		EventType:   "com.joustmania.acceleration",
		EventSource: "controller-manager",
		MetricName:  "acceleration",
		MetricValue: 2.5,
		Serial:      "abc",
		Timestamp:   "2026-01-15T12:00:00Z",
	}

	data, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("failed to marshal: %v", err)
	}

	// Verify dot-notation field names in JSON output.
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("failed to unmarshal: %v", err)
	}

	expectedKeys := []string{"event.type", "event.source", "metric.name", "metric.value", "serial", "timestamp"}
	for _, key := range expectedKeys {
		if _, ok := raw[key]; !ok {
			t.Errorf("missing JSON key %q", key)
		}
	}

	if raw["event.type"] != "com.joustmania.acceleration" {
		t.Errorf("event.type = %v, want %q", raw["event.type"], "com.joustmania.acceleration")
	}
	if raw["event.source"] != "controller-manager" {
		t.Errorf("event.source = %v, want %q", raw["event.source"], "controller-manager")
	}
	if raw["metric.value"].(float64) != 2.5 {
		t.Errorf("metric.value = %v, want 2.5", raw["metric.value"])
	}
}

func TestConsumeMetrics_SumMetricType(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	ts := time.Date(2026, 1, 15, 12, 0, 0, 0, time.UTC)

	md := pmetric.NewMetrics()
	rm := md.ResourceMetrics().AppendEmpty()
	rm.Resource().Attributes().PutStr("service.name", "test-svc")
	sm := rm.ScopeMetrics().AppendEmpty()
	m := sm.Metrics().AppendEmpty()
	m.SetName("counter_total")
	s := m.SetEmptySum()
	dp := s.DataPoints().AppendEmpty()
	dp.SetIntValue(42)
	dp.Attributes().PutStr("serial", "s1")
	dp.SetTimestamp(pcommon.NewTimestampFromTime(ts))

	err := exp.ConsumeMetrics(context.Background(), md)
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()

	if len(exp.buffer) != 1 {
		t.Fatalf("expected 1 event, got %d", len(exp.buffer))
	}
	if exp.buffer[0].MetricValue != 42.0 {
		t.Errorf("metric.value = %f, want 42.0", exp.buffer[0].MetricValue)
	}
	if exp.buffer[0].MetricName != "counter_total" {
		t.Errorf("metric.name = %q, want %q", exp.buffer[0].MetricName, "counter_total")
	}
}

// Ensure ConsumeMetrics always returns nil even when called with empty metrics.
func TestConsumeMetrics_EmptyMetrics(t *testing.T) {
	logger := zaptest.NewLogger(t)
	cfg := &Config{Endpoint: "", BatchIntervalMS: 1000}
	exp := newExporter(cfg, logger)

	err := exp.ConsumeMetrics(context.Background(), pmetric.NewMetrics())
	if err != nil {
		t.Fatalf("ConsumeMetrics returned error for empty metrics: %v", err)
	}

	exp.mu.Lock()
	defer exp.mu.Unlock()
	if len(exp.buffer) != 0 {
		t.Errorf("expected empty buffer, got %d events", len(exp.buffer))
	}
}
