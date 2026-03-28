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
}

// NewFlagdSampler creates a new sampler that reads from flagd.
func NewFlagdSampler() *FlagdSampler {
	s := &FlagdSampler{}
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
	// Clamp to avoid uint64 overflow for rates near 1.0
	var threshold uint64
	if rate >= 1.0 {
		threshold = math.MaxUint64
	} else {
		threshold = uint64(rate * float64(math.MaxUint64))
	}

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
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				client := getFlagClient()
				rate, err := client.FloatValue(ctx, "trace_sampling_rate", 1.0, openfeature.EvaluationContext{})
				if err != nil {
					slog.Debug("flagd: failed to read trace_sampling_rate", "error", err)
					continue
				}
				s.cachedRate.Store(math.Float64bits(rate))
			}
		}
	}()
}
