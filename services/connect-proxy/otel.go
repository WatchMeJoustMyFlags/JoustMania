package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"runtime"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/sdk/metric"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// initLogs sets up OTEL log export via OTLP and returns an slog.Logger backed by
// the OpenTelemetry bridge, plus a shutdown function. When OTEL_EXPORTER_OTLP_ENDPOINT
// is not set, returns slog.Default() and a no-op shutdown.
func initLogs(ctx context.Context) (*slog.Logger, func(context.Context) error) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		return slog.Default(), func(context.Context) error { return nil }
	}

	serviceName := getEnv("OTEL_SERVICE_NAME", "connect-proxy")
	namespace := getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure")

	// Strip http:// prefix for gRPC endpoint
	endpoint = strings.TrimPrefix(endpoint, "http://")

	exporter, err := otlploggrpc.New(ctx,
		otlploggrpc.WithEndpoint(endpoint),
		otlploggrpc.WithInsecure(),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL log exporter: %v\n", err)
		return slog.Default(), func(context.Context) error { return nil }
	}

	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceName(serviceName),
			semconv.ServiceNamespace(namespace),
		),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL resource for logs: %v\n", err)
		return slog.Default(), func(context.Context) error { return nil }
	}

	provider := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
		sdklog.WithResource(res),
	)

	handler := otelslog.NewHandler("connect-proxy", otelslog.WithLoggerProvider(provider))
	logger := slog.New(handler)

	fmt.Println("OTEL logs initialized")
	return logger, provider.Shutdown
}

// initMetrics sets up OTEL metrics with process metrics pushed to the collector.
// Returns a shutdown function. If OTEL_EXPORTER_OTLP_ENDPOINT is not set, returns a no-op.
func initMetrics(ctx context.Context) func(context.Context) error {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		return func(context.Context) error { return nil }
	}

	serviceName := getEnv("OTEL_SERVICE_NAME", "connect-proxy")
	namespace := getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure")

	// Strip http:// prefix for gRPC endpoint
	endpoint = strings.TrimPrefix(endpoint, "http://")

	exporter, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithEndpoint(endpoint),
		otlpmetricgrpc.WithInsecure(),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL metrics exporter: %v\n", err)
		return func(context.Context) error { return nil }
	}

	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceName(serviceName),
			semconv.ServiceNamespace(namespace),
		),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL resource: %v\n", err)
		return func(context.Context) error { return nil }
	}

	reader := metric.NewPeriodicReader(exporter, metric.WithInterval(1*time.Second))

	provider := metric.NewMeterProvider(
		metric.WithReader(reader),
		metric.WithResource(res),
	)

	meter := provider.Meter("process")

	// process_cpu_seconds_total — reads from /proc/self/stat
	// Must be ObservableCounter (not Gauge) to match Python services' Counter type.
	// CPU time is monotonically increasing, so Counter is semantically correct.
	_, _ = meter.Float64ObservableCounter("process_cpu_seconds_total",
		otelmetric.WithDescription("Total user and system CPU time spent in seconds"),
		otelmetric.WithFloat64Callback(func(_ context.Context, o otelmetric.Float64Observer) error {
			secs, err := readCPUSeconds()
			if err == nil {
				o.Observe(secs)
			}
			return nil
		}),
	)

	// process_resident_memory_bytes — from runtime.ReadMemStats (Sys approximates RSS)
	_, _ = meter.Int64ObservableGauge("process_resident_memory_bytes",
		otelmetric.WithDescription("Resident memory size in bytes"),
		otelmetric.WithInt64Callback(func(_ context.Context, o otelmetric.Int64Observer) error {
			bytes, err := readRSSBytes()
			if err == nil {
				o.Observe(bytes)
			}
			return nil
		}),
	)

	// process_threads — Go goroutines mapped to OS threads
	_, _ = meter.Int64ObservableGauge("process_threads",
		otelmetric.WithDescription("Number of active threads"),
		otelmetric.WithInt64Callback(func(_ context.Context, o otelmetric.Int64Observer) error {
			o.Observe(int64(runtime.NumGoroutine()))
			return nil
		}),
	)

	fmt.Println("OTEL metrics initialized (1s export interval)")
	return provider.Shutdown
}

// parseProcStat reads /proc/self/stat and returns fields after the comm field.
// The comm field (field 2) is enclosed in parens and may contain spaces,
// so we find the last ')' and split from there.
func parseProcStat() ([]string, error) {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return nil, err
	}
	// Find the end of the comm field (last closing paren)
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return nil, fmt.Errorf("unexpected /proc/self/stat format")
	}
	// Fields after ") " start at index 0 = state (field 3 in proc(5))
	fields := strings.Fields(s[idx+2:])
	return fields, nil
}

// readCPUSeconds reads cumulative CPU seconds from /proc/self/stat.
func readCPUSeconds() (float64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
	// After comm: index 11 = utime (field 14), index 12 = stime (field 15)
	// Fields: state(0) ppid(1) pgrp(2) session(3) tty_nr(4) tpgid(5) flags(6)
	//         minflt(7) cminflt(8) majflt(9) cmajflt(10) utime(11) stime(12) ...
	if len(fields) < 13 {
		return 0, fmt.Errorf("not enough fields in /proc/self/stat")
	}
	utime, err := strconv.ParseFloat(fields[11], 64)
	if err != nil {
		return 0, err
	}
	stime, err := strconv.ParseFloat(fields[12], 64)
	if err != nil {
		return 0, err
	}
	// Clock ticks per second (typically 100 on Linux)
	clkTck := 100.0
	return (utime + stime) / clkTck, nil
}

// readRSSBytes reads resident set size from /proc/self/stat.
func readRSSBytes() (int64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
	// After comm: index 21 = rss (field 24 in proc(5))
	if len(fields) < 22 {
		return 0, fmt.Errorf("not enough fields in /proc/self/stat")
	}
	rss, err := strconv.ParseInt(fields[21], 10, 64)
	if err != nil {
		return 0, err
	}
	return rss * int64(os.Getpagesize()), nil
}

