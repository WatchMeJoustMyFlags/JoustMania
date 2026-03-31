package main

import (
	"context"
	"log"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
)

// Package-level metric instruments, initialized in initMetrics().
var (
	mobileConnectionsActive metric.Int64UpDownCounter
	mobileConnectionsTotal  metric.Int64Counter
	mobileWSLatencySeconds  metric.Float64Histogram
)

// initMetrics sets up OTEL push metrics.
// Returns a shutdown function to flush metrics on exit.
func initMetrics() func() {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		// No OTEL endpoint configured; use noop meter
		meter := otel.Meter("mobile-gateway")
		registerInstruments(meter)
		return func() {}
	}

	// Strip http:// scheme if present; otlpmetricgrpc expects host:port
	cleanEndpoint := strings.TrimPrefix(endpoint, "http://")
	cleanEndpoint = strings.TrimPrefix(cleanEndpoint, "https://")

	ctx := context.Background()

	exporter, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithEndpoint(cleanEndpoint),
		otlpmetricgrpc.WithInsecure(),
	)
	if err != nil {
		log.Printf("Failed to create OTEL metrics exporter: %v (using noop)", err)
		meter := otel.Meter("mobile-gateway")
		registerInstruments(meter)
		return func() {}
	}

	provider := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(
			sdkmetric.NewPeriodicReader(exporter, sdkmetric.WithInterval(15*time.Second)),
		),
	)
	otel.SetMeterProvider(provider)

	meter := provider.Meter("mobile-gateway")
	registerInstruments(meter)

	return func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if err := provider.Shutdown(shutdownCtx); err != nil {
			log.Printf("OTEL metrics shutdown error: %v", err)
		}
	}
}

func registerInstruments(meter metric.Meter) {
	var err error

	mobileConnectionsActive, err = meter.Int64UpDownCounter(
		"mobile_connections_active",
		metric.WithDescription("Number of currently active phone WebSocket connections"),
	)
	if err != nil {
		log.Printf("Failed to create mobile_connections_active metric: %v", err)
	}

	mobileConnectionsTotal, err = meter.Int64Counter(
		"mobile_connections_total",
		metric.WithDescription("Total phone WebSocket connections established"),
	)
	if err != nil {
		log.Printf("Failed to create mobile_connections_total metric: %v", err)
	}

	mobileWSLatencySeconds, err = meter.Float64Histogram(
		"mobile_ws_latency_seconds",
		metric.WithDescription("WebSocket message processing latency"),
		metric.WithExplicitBucketBoundaries(0.001, 0.005, 0.010, 0.016, 0.025, 0.050, 0.100),
	)
	if err != nil {
		log.Printf("Failed to create mobile_ws_latency_seconds metric: %v", err)
	}
}
