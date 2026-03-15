// Copyright 2026 JoustMania Authors
// SPDX-License-Identifier: Apache-2.0

package dtbizeventsexporter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"go.opentelemetry.io/collector/component"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.uber.org/zap"
)

// Bizevent represents a Dynatrace business event derived from a metric data point.
type Bizevent struct {
	EventType   string  `json:"event.type"`
	EventSource string  `json:"event.source"`
	MetricName  string  `json:"metric.name"`
	MetricValue float64 `json:"metric.value"`
	Serial      string  `json:"serial"`
	Timestamp   string  `json:"timestamp"`
}

type bizeventsExporter struct {
	cfg    *Config
	logger *zap.Logger
	client *http.Client

	mu     sync.Mutex
	buffer []Bizevent

	stopCh chan struct{}
	wg     sync.WaitGroup
}

func newExporter(cfg *Config, logger *zap.Logger) *bizeventsExporter {
	return &bizeventsExporter{
		cfg:    cfg,
		logger: logger,
		buffer: make([]Bizevent, 0, 128),
		stopCh: make(chan struct{}),
	}
}

// Start initialises the HTTP client and starts the background flush goroutine.
func (e *bizeventsExporter) Start(_ context.Context, _ component.Host) error {
	e.client = &http.Client{Timeout: 10 * time.Second}

	e.wg.Add(1)
	go e.batchLoop()

	return nil
}

// Shutdown stops the flush goroutine and sends any remaining buffered events.
func (e *bizeventsExporter) Shutdown(_ context.Context) error {
	close(e.stopCh)
	e.wg.Wait()

	// Flush remaining events.
	e.mu.Lock()
	remaining := e.drainBufferLocked()
	e.mu.Unlock()

	if len(remaining) > 0 {
		e.sendBatch(remaining)
	}

	return nil
}

// ConsumeMetrics converts metric data points to bizevents and buffers them.
// It always returns nil (best-effort, no backpressure).
func (e *bizeventsExporter) ConsumeMetrics(_ context.Context, md pmetric.Metrics) error {
	var events []Bizevent

	for ri := 0; ri < md.ResourceMetrics().Len(); ri++ {
		rm := md.ResourceMetrics().At(ri)
		serviceName := "unknown"
		if v, ok := rm.Resource().Attributes().Get("service.name"); ok {
			serviceName = v.AsString()
		}

		for si := 0; si < rm.ScopeMetrics().Len(); si++ {
			sm := rm.ScopeMetrics().At(si)
			for mi := 0; mi < sm.Metrics().Len(); mi++ {
				m := sm.Metrics().At(mi)
				events = append(events, e.extractDataPoints(m, serviceName)...)
			}
		}
	}

	if len(events) > 0 {
		e.mu.Lock()
		e.buffer = append(e.buffer, events...)
		e.mu.Unlock()
	}

	return nil
}

func (e *bizeventsExporter) extractDataPoints(m pmetric.Metric, serviceName string) []Bizevent {
	var events []Bizevent

	switch m.Type() {
	case pmetric.MetricTypeGauge:
		dps := m.Gauge().DataPoints()
		for i := 0; i < dps.Len(); i++ {
			events = append(events, e.dataPointToBizevent(m.Name(), serviceName, dps.At(i)))
		}
	case pmetric.MetricTypeSum:
		dps := m.Sum().DataPoints()
		for i := 0; i < dps.Len(); i++ {
			events = append(events, e.dataPointToBizevent(m.Name(), serviceName, dps.At(i)))
		}
	default:
		// Histogram, summary, etc. are not supported for bizevents conversion.
	}

	return events
}

func (e *bizeventsExporter) dataPointToBizevent(
	metricName, serviceName string,
	dp pmetric.NumberDataPoint,
) Bizevent {
	serial := ""
	if v, ok := dp.Attributes().Get("serial"); ok {
		serial = v.AsString()
	}

	var value float64
	switch dp.ValueType() {
	case pmetric.NumberDataPointValueTypeDouble:
		value = dp.DoubleValue()
	case pmetric.NumberDataPointValueTypeInt:
		value = float64(dp.IntValue())
	}

	ts := dp.Timestamp().AsTime().Format(time.RFC3339Nano)

	return Bizevent{
		EventType:   "com.joustmania.acceleration",
		EventSource: serviceName,
		MetricName:  metricName,
		MetricValue: value,
		Serial:      serial,
		Timestamp:   ts,
	}
}

func (e *bizeventsExporter) batchLoop() {
	defer e.wg.Done()

	ticker := time.NewTicker(time.Duration(e.cfg.BatchIntervalMS) * time.Millisecond)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			e.mu.Lock()
			batch := e.drainBufferLocked()
			e.mu.Unlock()

			if len(batch) > 0 {
				e.sendBatch(batch)
			}
		case <-e.stopCh:
			return
		}
	}
}

// drainBufferLocked returns the current buffer and replaces it with a fresh one.
// Caller must hold e.mu.
func (e *bizeventsExporter) drainBufferLocked() []Bizevent {
	if len(e.buffer) == 0 {
		return nil
	}
	batch := e.buffer
	e.buffer = make([]Bizevent, 0, 128)
	return batch
}

func (e *bizeventsExporter) sendBatch(events []Bizevent) {
	if e.cfg.Endpoint == "" {
		e.logger.Info("dry run: would send bizevents", zap.Int("count", len(events)))
		return
	}

	body, err := json.Marshal(events)
	if err != nil {
		e.logger.Error("failed to marshal bizevents", zap.Error(err))
		return
	}

	req, err := http.NewRequest(http.MethodPost, e.cfg.Endpoint, bytes.NewReader(body))
	if err != nil {
		e.logger.Error("failed to create request", zap.Error(err))
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", fmt.Sprintf("Api-Token %s", e.cfg.APIToken))

	resp, err := e.client.Do(req)
	if err != nil {
		e.logger.Error("failed to send bizevents", zap.Error(err))
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 400 {
		e.logger.Error("bizevents API returned error",
			zap.Int("status", resp.StatusCode),
			zap.Int("batch_size", len(events)),
		)
	} else {
		e.logger.Debug("sent bizevents batch",
			zap.Int("status", resp.StatusCode),
			zap.Int("batch_size", len(events)),
		)
	}
}
