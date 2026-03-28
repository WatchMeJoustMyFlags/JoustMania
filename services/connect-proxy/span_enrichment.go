package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"runtime"
	"sync"
	"sync/atomic"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Cached values to avoid syscalls on every span.
var (
	cachedHostname    = sync.OnceValue(func() string { h, _ := os.Hostname(); return h })
	cachedServiceName = sync.OnceValue(func() string { return os.Getenv("OTEL_SERVICE_NAME") })
	cachedServiceNS   = sync.OnceValue(func() string { return os.Getenv("OTEL_SERVICE_NAMESPACE") })
)

// SpanEnrichmentProcessor adds diagnostic attributes to spans based on flagd config.
type SpanEnrichmentProcessor struct {
	enabled atomic.Bool
	sets    atomic.Uint32
	client  openfeature.IClient
}

const (
	setServiceIdentity uint32 = 1 << iota
	setFlagValues
	setRuntime
	setSystem
)

// NewSpanEnrichmentProcessor creates a new enrichment processor.
func NewSpanEnrichmentProcessor() *SpanEnrichmentProcessor {
	return &SpanEnrichmentProcessor{
		client: getFlagClient(),
	}
}

// OnStart adds enrichment attributes to spans.
func (p *SpanEnrichmentProcessor) OnStart(parent context.Context, s sdktrace.ReadWriteSpan) {
	if !p.enabled.Load() {
		return
	}

	sets := p.sets.Load()
	if sets == 0 {
		return
	}

	if sets&setServiceIdentity != 0 {
		s.SetAttributes(
			attribute.Bool("demo.enriched", true),
			attribute.String("demo.language", "go"),
			attribute.String("demo.service", cachedServiceName()),
			attribute.String("demo.namespace", cachedServiceNS()),
		)
	}

	if sets&setRuntime != 0 {
		s.SetAttributes(
			attribute.String("demo.runtime.version", runtime.Version()),
			attribute.Int("demo.runtime.pid", os.Getpid()),
			attribute.Int("demo.runtime.threads", runtime.NumGoroutine()),
		)
	}

	if sets&setSystem != 0 {
		s.SetAttributes(
			attribute.String("demo.system.hostname", cachedHostname()),
			attribute.Int("demo.system.cpu_count", runtime.NumCPU()),
		)
	}
}

// OnEnd is a no-op.
func (p *SpanEnrichmentProcessor) OnEnd(s sdktrace.ReadOnlySpan) {}

// Shutdown is a no-op.
func (p *SpanEnrichmentProcessor) Shutdown(ctx context.Context) error { return nil }

// ForceFlush is a no-op.
func (p *SpanEnrichmentProcessor) ForceFlush(ctx context.Context) error { return nil }

// StartConfigUpdater spawns a goroutine that polls flagd every 2 seconds.
func (p *SpanEnrichmentProcessor) StartConfigUpdater(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				p.updateConfig(ctx)
			}
		}
	}()
}

func (p *SpanEnrichmentProcessor) updateConfig(ctx context.Context) {
	// Get the structured flag value as a JSON-like interface{}
	val, err := p.client.ObjectValue(ctx, "span_enrichment_config", map[string]interface{}{
		"enabled":        false,
		"attribute_sets": []interface{}{},
	}, openfeature.EvaluationContext{})
	if err != nil {
		slog.Debug("flagd: failed to read span_enrichment_config", "error", err)
		return
	}

	// Parse the value
	data, err := json.Marshal(val)
	if err != nil {
		return
	}

	var config struct {
		Enabled       bool     `json:"enabled"`
		AttributeSets []string `json:"attribute_sets"`
	}
	if err := json.Unmarshal(data, &config); err != nil {
		return
	}

	p.enabled.Store(config.Enabled)

	var sets uint32
	for _, s := range config.AttributeSets {
		switch s {
		case "service_identity":
			sets |= setServiceIdentity
		case "flag_values":
			sets |= setFlagValues
		case "runtime":
			sets |= setRuntime
		case "system":
			sets |= setSystem
		}
	}
	p.sets.Store(sets)
}
