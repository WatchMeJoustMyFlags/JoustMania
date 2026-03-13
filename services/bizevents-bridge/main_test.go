package main

import (
	"compress/gzip"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestParseOTLPLogs_BasicConversion(t *testing.T) {
	metricName := "controller_accel_magnitude"
	metricValue := 2.5
	serial := "00:1A:7D:DA:71:A5"
	serviceName := "controller-manager-service"
	timeNano := "1710331200123000000" // 2024-03-13T12:00:00.123Z

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &serviceName}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								TimeUnixNano: timeNano,
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &metricName}},
									{Key: "metric.value", Value: AnyValue{DoubleValue: &metricValue}},
									{Key: "serial", Value: AnyValue{StringValue: &serial}},
								},
							},
						},
					},
				},
			},
		},
	}

	data, err := json.Marshal(input)
	if err != nil {
		t.Fatalf("Failed to marshal input: %v", err)
	}

	events, err := parseOTLPLogs(data)
	if err != nil {
		t.Fatalf("parseOTLPLogs failed: %v", err)
	}

	if len(events) != 1 {
		t.Fatalf("Expected 1 event, got %d", len(events))
	}

	ev := events[0]
	if ev.EventType != "com.joustmania.acceleration" {
		t.Errorf("EventType = %q, want %q", ev.EventType, "com.joustmania.acceleration")
	}
	if ev.EventSource != "controller-manager-service" {
		t.Errorf("EventSource = %q, want %q", ev.EventSource, "controller-manager-service")
	}
	if ev.MetricName != "controller_accel_magnitude" {
		t.Errorf("MetricName = %q, want %q", ev.MetricName, "controller_accel_magnitude")
	}
	if ev.MetricValue != 2.5 {
		t.Errorf("MetricValue = %f, want 2.5", ev.MetricValue)
	}
	if ev.Serial != "00:1A:7D:DA:71:A5" {
		t.Errorf("Serial = %q, want %q", ev.Serial, "00:1A:7D:DA:71:A5")
	}
	if ev.Timestamp == "" {
		t.Error("Timestamp is empty")
	}
}

func TestParseOTLPLogs_BodyKVList(t *testing.T) {
	// Test when metric info is in the body kvlist
	metricName := "controller_accel_x"
	metricValue := 1.23
	serial := "AA:BB:CC:DD:EE:FF"
	serviceName := "controller-manager-service"

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &serviceName}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								TimeUnixNano: "1710331200000000000",
								Body: &AnyValue{
									KvlistValue: &KVList{
										Values: []Attribute{
											{Key: "metric.name", Value: AnyValue{StringValue: &metricName}},
											{Key: "metric.value", Value: AnyValue{DoubleValue: &metricValue}},
											{Key: "serial", Value: AnyValue{StringValue: &serial}},
										},
									},
								},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)
	events, err := parseOTLPLogs(data)
	if err != nil {
		t.Fatalf("parseOTLPLogs failed: %v", err)
	}

	if len(events) != 1 {
		t.Fatalf("Expected 1 event, got %d", len(events))
	}

	if events[0].MetricName != "controller_accel_x" {
		t.Errorf("MetricName = %q, want %q", events[0].MetricName, "controller_accel_x")
	}
	if events[0].MetricValue != 1.23 {
		t.Errorf("MetricValue = %f, want 1.23", events[0].MetricValue)
	}
}

func TestParseOTLPLogs_NoMetricName(t *testing.T) {
	// Log records without metric.name should be skipped
	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								TimeUnixNano: "1710331200000000000",
								Attributes:   []Attribute{},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)
	events, err := parseOTLPLogs(data)
	if err != nil {
		t.Fatalf("parseOTLPLogs failed: %v", err)
	}

	if len(events) != 0 {
		t.Errorf("Expected 0 events, got %d", len(events))
	}
}

func TestParseOTLPLogs_IntValue(t *testing.T) {
	// Test integer metric values (OTLP JSON encodes int64 as string)
	metricName := "controller_accel_magnitude"
	intVal := "42"
	serviceName := "test"

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &serviceName}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &metricName}},
									{Key: "metric.value", Value: AnyValue{IntValue: &intVal}},
								},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)
	events, err := parseOTLPLogs(data)
	if err != nil {
		t.Fatalf("parseOTLPLogs failed: %v", err)
	}

	if len(events) != 1 {
		t.Fatalf("Expected 1 event, got %d", len(events))
	}

	if events[0].MetricValue != 42.0 {
		t.Errorf("MetricValue = %f, want 42.0", events[0].MetricValue)
	}
}

func TestParseOTLPLogs_MultipleRecords(t *testing.T) {
	mn1 := "controller_accel_x"
	mn2 := "controller_accel_y"
	v1 := 1.0
	v2 := 2.0
	s1 := "AA:BB"
	s2 := "CC:DD"
	svc := "test"

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &svc}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &mn1}},
									{Key: "metric.value", Value: AnyValue{DoubleValue: &v1}},
									{Key: "serial", Value: AnyValue{StringValue: &s1}},
								},
							},
							{
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &mn2}},
									{Key: "metric.value", Value: AnyValue{DoubleValue: &v2}},
									{Key: "serial", Value: AnyValue{StringValue: &s2}},
								},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)
	events, err := parseOTLPLogs(data)
	if err != nil {
		t.Fatalf("parseOTLPLogs failed: %v", err)
	}

	if len(events) != 2 {
		t.Fatalf("Expected 2 events, got %d", len(events))
	}
}

func TestParseTimeUnixNano(t *testing.T) {
	tests := []struct {
		name  string
		input string
		check func(string) bool
	}{
		{
			name:  "valid nanoseconds",
			input: "1710331200123000000",
			check: func(s string) bool { return s == "2024-03-13T12:00:00.123Z" },
		},
		{
			name:  "empty string uses current time",
			input: "",
			check: func(s string) bool { return s != "" },
		},
		{
			name:  "zero uses current time",
			input: "0",
			check: func(s string) bool { return s != "" },
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := parseTimeUnixNano(tt.input)
			if !tt.check(result) {
				t.Errorf("parseTimeUnixNano(%q) = %q, unexpected", tt.input, result)
			}
		})
	}
}

func TestHandleLogs_Success(t *testing.T) {
	metricName := "controller_accel_magnitude"
	metricValue := 2.5
	svc := "test"

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &svc}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &metricName}},
									{Key: "metric.value", Value: AnyValue{DoubleValue: &metricValue}},
								},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)
	req := httptest.NewRequest(http.MethodPost, "/v1/logs", bytes.NewReader(data))
	req.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()

	handleLogs(w, req)

	resp := w.Result()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("Status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
}

func TestHandleLogs_GzipBody(t *testing.T) {
	metricName := "controller_accel_z"
	metricValue := 3.14
	svc := "test"

	input := OTLPLogsRequest{
		ResourceLogs: []ResourceLogs{
			{
				Resource: Resource{
					Attributes: []Attribute{
						{Key: "service.name", Value: AnyValue{StringValue: &svc}},
					},
				},
				ScopeLogs: []ScopeLogs{
					{
						LogRecords: []LogRecord{
							{
								Attributes: []Attribute{
									{Key: "metric.name", Value: AnyValue{StringValue: &metricName}},
									{Key: "metric.value", Value: AnyValue{DoubleValue: &metricValue}},
								},
							},
						},
					},
				},
			},
		},
	}

	data, _ := json.Marshal(input)

	var gzBuf bytes.Buffer
	gz := gzip.NewWriter(&gzBuf)
	gz.Write(data)
	gz.Close()

	req := httptest.NewRequest(http.MethodPost, "/v1/logs", &gzBuf)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Content-Encoding", "gzip")
	w := httptest.NewRecorder()

	handleLogs(w, req)

	resp := w.Result()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("Status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
}

func TestHandleLogs_MethodNotAllowed(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/logs", nil)
	w := httptest.NewRecorder()

	handleLogs(w, req)

	resp := w.Result()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Errorf("Status = %d, want %d", resp.StatusCode, http.StatusMethodNotAllowed)
	}
}

func TestHandleLogs_InvalidJSON(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/v1/logs", bytes.NewReader([]byte("not json")))
	w := httptest.NewRecorder()

	handleLogs(w, req)

	// Should return OK even on parse error (non-blocking)
	resp := w.Result()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("Status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
}

func TestHandleHealth(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	w := httptest.NewRecorder()

	handleHealth(w, req)

	resp := w.Result()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("Status = %d, want %d", resp.StatusCode, http.StatusOK)
	}
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "ok" {
		t.Errorf("Body = %q, want %q", string(body), "ok")
	}
}

func TestBizeventJSON(t *testing.T) {
	ev := Bizevent{
		EventType:   "com.joustmania.acceleration",
		EventSource: "controller-manager-service",
		MetricName:  "controller_accel_magnitude",
		MetricValue: 2.5,
		Serial:      "00:1A:7D:DA:71:A5",
		Timestamp:   "2026-03-13T12:00:00.123Z",
	}

	data, err := json.Marshal(ev)
	if err != nil {
		t.Fatalf("Failed to marshal bizevent: %v", err)
	}

	// Verify JSON field names use dot notation
	var m map[string]interface{}
	json.Unmarshal(data, &m)

	if _, ok := m["event.type"]; !ok {
		t.Error("Missing 'event.type' field in JSON")
	}
	if _, ok := m["event.source"]; !ok {
		t.Error("Missing 'event.source' field in JSON")
	}
	if _, ok := m["metric.name"]; !ok {
		t.Error("Missing 'metric.name' field in JSON")
	}
	if _, ok := m["metric.value"]; !ok {
		t.Error("Missing 'metric.value' field in JSON")
	}
	if m["metric.value"].(float64) != 2.5 {
		t.Errorf("metric.value = %v, want 2.5", m["metric.value"])
	}
}

func TestExtractResourceServiceName(t *testing.T) {
	svc := "my-service"
	res := Resource{
		Attributes: []Attribute{
			{Key: "service.name", Value: AnyValue{StringValue: &svc}},
		},
	}
	if name := extractResourceServiceName(res); name != "my-service" {
		t.Errorf("Got %q, want %q", name, "my-service")
	}

	// No service.name falls back to "unknown"
	if name := extractResourceServiceName(Resource{}); name != "unknown" {
		t.Errorf("Got %q, want %q", name, "unknown")
	}
}

func TestSendBatch_DryRun(t *testing.T) {
	// With empty endpoint, sendBatch should not panic
	origEndpoint := config.BizeventsEndpoint
	config.BizeventsEndpoint = ""
	defer func() { config.BizeventsEndpoint = origEndpoint }()

	events := []Bizevent{
		{
			EventType:   "com.joustmania.acceleration",
			EventSource: "test",
			MetricName:  "controller_accel_magnitude",
			MetricValue: 1.0,
		},
	}

	// Should not panic
	sendBatch(events)
}
