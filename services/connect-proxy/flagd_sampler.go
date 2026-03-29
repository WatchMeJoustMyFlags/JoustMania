package main

import (
	"context"
	"encoding/binary"
	"log/slog"
	"math"
	"sync/atomic"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// FlagdSampler reads trace_sampling_rate from flagd.
// Uses a cached rate updated by a background goroutine.
type FlagdSampler struct {
	cachedRate atomic.Uint64
	client     openfeature.IClient
}

// NewFlagdSampler creates a new sampler that reads from flagd.
func NewFlagdSampler() *FlagdSampler {
	s := &FlagdSampler{
		client: getFlagClient(),
	}
	s.cachedRate.Store(math.Float64bits(1.0)) // default: sample all
	return s
}

// ShouldSample implements sdktrace.Sampler.
func (s *FlagdSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	rate := math.Float64frombits(s.cachedRate.Load())

	if rate >= 1.0 {
		return sdktrace.SamplingResult{
			Decision:   sdktrace.RecordAndSample,
			Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState(),
		}
	}

	if rate <= 0.0 {
		return sdktrace.SamplingResult{
			Decision:   sdktrace.Drop,
			Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState(),
		}
	}

	// Deterministic sampling based on trace ID
	traceID := p.TraceID
	hash := binary.BigEndian.Uint64(traceID[8:16])
	threshold := uint64(rate * float64(math.MaxUint64))

	decision := sdktrace.Drop
	if hash < threshold {
		decision = sdktrace.RecordAndSample
	}

	return sdktrace.SamplingResult{
		Decision:   decision,
		Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState(),
	}
}

// Description implements sdktrace.Sampler.
func (s *FlagdSampler) Description() string {
	return "FlagdSampler"
}

// StartRateUpdater spawns a goroutine that polls flagd every 2 seconds.
func (s *FlagdSampler) StartRateUpdater(ctx context.Context) {
	go func() {
		// Fetch immediately on startup, then poll every 2s.
		rate, err := s.client.FloatValue(ctx, "trace_sampling_rate", 1.0, openfeature.EvaluationContext{})
		if err == nil {
			if !math.IsNaN(rate) {
				if rate < 0 {
					rate = 0
				} else if rate > 1 {
					rate = 1
				}
				s.cachedRate.Store(math.Float64bits(rate))
			}
		}

		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		failing := false
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				rate, err := s.client.FloatValue(ctx, "trace_sampling_rate", 1.0, openfeature.EvaluationContext{})
				if err != nil {
					slog.Debug("flagd: failed to read trace_sampling_rate", "error", err)
					failing = true
					continue
				}
				if failing {
					slog.Info("flagd: trace_sampling_rate recovered", "rate", rate)
					failing = false
				}
				// Validate: reject NaN and clamp to [0, 1]
				if math.IsNaN(rate) {
					slog.Warn("flagd: trace_sampling_rate is NaN, ignoring")
					continue
				}
				if rate < 0 {
					rate = 0
				} else if rate > 1 {
					rate = 1
				}
				s.cachedRate.Store(math.Float64bits(rate))
			}
		}
	}()
}
