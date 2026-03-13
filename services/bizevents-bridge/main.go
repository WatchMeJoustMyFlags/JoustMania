// Bizevents Bridge
//
// Receives OTLP log records (from metricsaslogs connector) and converts them
// to Dynatrace business events, POSTing to /api/v2/bizevents/ingest.
//
// The metricsaslogs connector converts metric data points into log records
// with the metric name, value, and dimensions as log body/attributes.
//
// Endpoints:
//   - POST /v1/logs - OTLP HTTP logs receiver (JSON format)
//   - GET /health   - Health check
//
// Environment variables:
//   - PORT: HTTP server port (default: 4319)
//   - DYNATRACE_BIZEVENTS_ENDPOINT: Dynatrace bizevents ingest URL
//   - DYNATRACE_BIZEVENTS_TOKEN: API token with bizevents.ingest scope
//   - BATCH_INTERVAL_MS: Batch flush interval in milliseconds (default: 1000)
package main

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"sync"
	"time"
)

// Config holds service configuration.
type Config struct {
	Port              string
	BizeventsEndpoint string
	BizeventsToken    string
	BatchIntervalMS   int
}

// Bizevent represents a single Dynatrace business event.
type Bizevent struct {
	EventType   string  `json:"event.type"`
	EventSource string  `json:"event.source"`
	MetricName  string  `json:"metric.name"`
	MetricValue float64 `json:"metric.value"`
	Serial      string  `json:"serial"`
	Timestamp   string  `json:"timestamp"`
}

// OTLP JSON log structures (subset needed for metricsaslogs output).
type OTLPLogsRequest struct {
	ResourceLogs []ResourceLogs `json:"resourceLogs"`
}

type ResourceLogs struct {
	Resource  Resource   `json:"resource"`
	ScopeLogs []ScopeLogs `json:"scopeLogs"`
}

type Resource struct {
	Attributes []Attribute `json:"attributes"`
}

type ScopeLogs struct {
	LogRecords []LogRecord `json:"logRecords"`
}

type LogRecord struct {
	TimeUnixNano string      `json:"timeUnixNano"`
	Body         *AnyValue   `json:"body"`
	Attributes   []Attribute `json:"attributes"`
}

type Attribute struct {
	Key   string   `json:"key"`
	Value AnyValue `json:"value"`
}

type AnyValue struct {
	StringValue *string  `json:"stringValue,omitempty"`
	DoubleValue *float64 `json:"doubleValue,omitempty"`
	IntValue    *string  `json:"intValue,omitempty"` // OTLP JSON encodes int64 as string
	KvlistValue *KVList  `json:"kvlistValue,omitempty"`
}

type KVList struct {
	Values []Attribute `json:"values"`
}

var (
	config     Config
	httpClient *http.Client
	eventBuf   []Bizevent
	eventMutex sync.Mutex
)

func main() {
	batchMS, _ := strconv.Atoi(getEnv("BATCH_INTERVAL_MS", "1000"))
	if batchMS <= 0 {
		batchMS = 1000
	}

	config = Config{
		Port:              getEnv("PORT", "4319"),
		BizeventsEndpoint: getEnv("DYNATRACE_BIZEVENTS_ENDPOINT", ""),
		BizeventsToken:    getEnv("DYNATRACE_BIZEVENTS_TOKEN", ""),
		BatchIntervalMS:   batchMS,
	}

	httpClient = &http.Client{
		Timeout: 10 * time.Second,
	}

	// Start background batch sender
	go batchLoop()

	http.HandleFunc("/v1/logs", handleLogs)
	http.HandleFunc("/health", handleHealth)

	addr := ":" + config.Port
	log.Printf("Bizevents Bridge starting on %s", addr)
	log.Printf("  Dynatrace endpoint: %s", config.BizeventsEndpoint)
	log.Printf("  Batch interval: %dms", config.BatchIntervalMS)

	if config.BizeventsEndpoint == "" {
		log.Printf("  WARNING: DYNATRACE_BIZEVENTS_ENDPOINT not set, events will be logged but not sent")
	}

	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("Server failed: %v", err)
	}
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte("ok"))
}

func handleLogs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := readBody(r)
	if err != nil {
		http.Error(w, "Failed to read body", http.StatusBadRequest)
		return
	}
	defer r.Body.Close()

	events, err := parseOTLPLogs(body)
	if err != nil {
		log.Printf("Failed to parse OTLP logs: %v", err)
		// Return OK to not backpressure the collector
		w.WriteHeader(http.StatusOK)
		return
	}

	if len(events) > 0 {
		eventMutex.Lock()
		eventBuf = append(eventBuf, events...)
		eventMutex.Unlock()
	}

	w.WriteHeader(http.StatusOK)
}

func readBody(r *http.Request) ([]byte, error) {
	if r.Header.Get("Content-Encoding") == "gzip" {
		reader, err := gzip.NewReader(r.Body)
		if err != nil {
			return nil, fmt.Errorf("gzip decompress: %w", err)
		}
		defer reader.Close()
		return io.ReadAll(reader)
	}
	return io.ReadAll(r.Body)
}

// parseOTLPLogs extracts bizevents from OTLP log records produced by
// the metricsaslogs connector. Each log record represents a single metric
// data point with attributes containing the metric name, value, and dimensions.
func parseOTLPLogs(data []byte) ([]Bizevent, error) {
	var req OTLPLogsRequest
	if err := json.Unmarshal(data, &req); err != nil {
		return nil, fmt.Errorf("json unmarshal: %w", err)
	}

	var events []Bizevent

	for _, rl := range req.ResourceLogs {
		serviceName := extractResourceServiceName(rl.Resource)

		for _, sl := range rl.ScopeLogs {
			for _, lr := range sl.LogRecords {
				event := logRecordToBizevent(lr, serviceName)
				if event != nil {
					events = append(events, *event)
				}
			}
		}
	}

	return events, nil
}

// extractResourceServiceName finds service.name from resource attributes.
func extractResourceServiceName(res Resource) string {
	for _, attr := range res.Attributes {
		if attr.Key == "service.name" && attr.Value.StringValue != nil {
			return *attr.Value.StringValue
		}
	}
	return "unknown"
}

// logRecordToBizevent converts a single OTLP log record (from metricsaslogs)
// into a Dynatrace bizevent. Returns nil if the record can't be converted.
func logRecordToBizevent(lr LogRecord, serviceName string) *Bizevent {
	var metricName string
	var metricValue float64
	var serial string

	// The metricsaslogs connector puts metric info in log record attributes
	for _, attr := range lr.Attributes {
		switch attr.Key {
		case "metric.name":
			if attr.Value.StringValue != nil {
				metricName = *attr.Value.StringValue
			}
		case "metric.value":
			metricValue = anyValueToFloat64(attr.Value)
		case "serial":
			if attr.Value.StringValue != nil {
				serial = *attr.Value.StringValue
			}
		}
	}

	// Also check body for metric info (connector may use body or attributes)
	if lr.Body != nil && lr.Body.KvlistValue != nil {
		for _, kv := range lr.Body.KvlistValue.Values {
			switch kv.Key {
			case "metric.name":
				if kv.Value.StringValue != nil && metricName == "" {
					metricName = *kv.Value.StringValue
				}
			case "metric.value":
				if metricValue == 0 {
					metricValue = anyValueToFloat64(kv.Value)
				}
			case "serial":
				if kv.Value.StringValue != nil && serial == "" {
					serial = *kv.Value.StringValue
				}
			}
		}
	}

	if metricName == "" {
		return nil
	}

	ts := parseTimeUnixNano(lr.TimeUnixNano)

	return &Bizevent{
		EventType:   "com.joustmania.acceleration",
		EventSource: serviceName,
		MetricName:  metricName,
		MetricValue: metricValue,
		Serial:      serial,
		Timestamp:   ts,
	}
}

func anyValueToFloat64(v AnyValue) float64 {
	if v.DoubleValue != nil {
		return *v.DoubleValue
	}
	if v.IntValue != nil {
		n, err := strconv.ParseInt(*v.IntValue, 10, 64)
		if err == nil {
			return float64(n)
		}
	}
	return 0
}

func parseTimeUnixNano(s string) string {
	if s == "" {
		return time.Now().UTC().Format(time.RFC3339Nano)
	}
	nanos, err := strconv.ParseInt(s, 10, 64)
	if err != nil || nanos == 0 {
		return time.Now().UTC().Format(time.RFC3339Nano)
	}
	t := time.Unix(0, nanos).UTC()
	return t.Format(time.RFC3339Nano)
}

func batchLoop() {
	ticker := time.NewTicker(time.Duration(config.BatchIntervalMS) * time.Millisecond)
	defer ticker.Stop()

	for range ticker.C {
		eventMutex.Lock()
		if len(eventBuf) == 0 {
			eventMutex.Unlock()
			continue
		}

		toSend := eventBuf
		eventBuf = nil
		eventMutex.Unlock()

		sendBatch(toSend)
	}
}

func sendBatch(events []Bizevent) {
	if config.BizeventsEndpoint == "" {
		log.Printf("Batch of %d bizevents (dry run, no endpoint configured)", len(events))
		return
	}

	// Dynatrace bizevents/ingest accepts a JSON array
	body, err := json.Marshal(events)
	if err != nil {
		log.Printf("Failed to marshal bizevents: %v", err)
		return
	}

	req, err := http.NewRequest("POST", config.BizeventsEndpoint, bytes.NewReader(body))
	if err != nil {
		log.Printf("Failed to create request: %v", err)
		return
	}

	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Api-Token "+config.BizeventsToken)

	resp, err := httpClient.Do(req)
	if err != nil {
		log.Printf("Failed to send bizevents batch (%d events): %v", len(events), err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		respBody, _ := io.ReadAll(resp.Body)
		log.Printf("Dynatrace bizevents ingest failed: %d - %s", resp.StatusCode, string(respBody))
	} else {
		log.Printf("Sent %d bizevents to Dynatrace", len(events))
	}
}
