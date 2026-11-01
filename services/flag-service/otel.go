package main

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetricgrpc"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// OTel setup, following services/agent/otel.go: service flag-service in
// namespace infrastructure, OTLP gRPC export to OTEL_EXPORTER_OTLP_ENDPOINT,
// metrics every 1 s (telemetry.md §10). Without an endpoint both are no-ops.

func resolveHostName() string {
	if attrs := os.Getenv("OTEL_RESOURCE_ATTRIBUTES"); attrs != "" {
		for _, pair := range strings.Split(attrs, ",") {
			if k, v, ok := strings.Cut(pair, "="); ok && strings.TrimSpace(k) == "host.name" {
				return strings.TrimSpace(v)
			}
		}
	}
	h, _ := os.Hostname()
	return h
}

var (
	instanceIDOnce sync.Once
	instanceID     string
)

// serviceInstanceID is service.instance.id, unique per process:
// "<HOSTNAME>-<8 hex>" (#2546, same rule as lib/otel_resource.py, #2543).
// Drawn once so traces, metrics and logs share it. The container id alone is
// not unique (two hosts booted from the same Docker state share it), which
// interleaved their counters into one series.
func serviceInstanceID(fallbackHost string) string {
	instanceIDOnce.Do(func() {
		instanceID = newInstanceID(getEnv("HOSTNAME", fallbackHost))
	})
	return instanceID
}

// newInstanceID appends a fresh random suffix to prefix.
func newInstanceID(prefix string) string {
	var b [4]byte
	if _, err := rand.Read(b[:]); err != nil {
		// crypto/rand failing is vanishingly rare; the pid still separates processes on a host.
		binary.BigEndian.PutUint32(b[:], uint32(os.Getpid())^uint32(time.Now().UnixNano()))
	}
	return prefix + "-" + hex.EncodeToString(b[:])
}

func newOTELResource(ctx context.Context, serviceName, namespace string) (*resource.Resource, error) {
	hostname := resolveHostName()
	return resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceName(serviceName),
			semconv.ServiceNamespace(namespace),
			semconv.ServiceVersion(getEnv("SERVICE_VERSION", "0.0.0-dev")),
			semconv.ServiceInstanceID(serviceInstanceID(hostname)),
			attribute.String("deployment.environment", getEnv("DEPLOYMENT_ENVIRONMENT", "development")),
			semconv.HostName(hostname),
			semconv.ContainerName(serviceName),
			attribute.Int("process.pid", os.Getpid()),
			attribute.String("process.executable.name", defaultServiceName),
			attribute.String("process.runtime.name", "go"),
			attribute.String("process.runtime.version", runtime.Version()),
		),
	)
}

func otlpEndpoint() string {
	return strings.TrimPrefix(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"), "http://")
}

func initTracing(ctx context.Context) func(context.Context) error {
	noop := func(context.Context) error { return nil }
	// The propagator is set even without an exporter so a write still joins the
	// caller's trace context in logs and child spans.
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	endpoint := otlpEndpoint()
	if endpoint == "" {
		return noop
	}
	exporter, err := otlptracegrpc.New(ctx, otlptracegrpc.WithEndpoint(endpoint), otlptracegrpc.WithInsecure())
	if err != nil {
		slog.Warn("OTel trace exporter unavailable", "error", err)
		return noop
	}
	res, err := newOTELResource(ctx, resolveServiceName(), getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure"))
	if err != nil {
		slog.Warn("OTel resource unavailable", "error", err)
		return noop
	}
	provider := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exporter), sdktrace.WithResource(res))
	otel.SetTracerProvider(provider)
	return provider.Shutdown
}

func initMetrics(ctx context.Context) func(context.Context) error {
	noop := func(context.Context) error { return nil }
	endpoint := otlpEndpoint()
	if endpoint == "" {
		return noop
	}
	exporter, err := otlpmetricgrpc.New(ctx, otlpmetricgrpc.WithEndpoint(endpoint), otlpmetricgrpc.WithInsecure())
	if err != nil {
		slog.Warn("OTel metric exporter unavailable", "error", err)
		return noop
	}
	res, err := newOTELResource(ctx, resolveServiceName(), getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure"))
	if err != nil {
		slog.Warn("OTel resource unavailable", "error", err)
		return noop
	}
	provider := metric.NewMeterProvider(
		metric.WithReader(metric.NewPeriodicReader(exporter, metric.WithInterval(time.Second))),
		metric.WithResource(res),
	)
	otel.SetMeterProvider(provider)
	registerProcessMetrics(provider.Meter("process"))
	return provider.Shutdown
}

// registerProcessMetrics publishes the standard process metrics every service
// exports (process_cpu_seconds_total, process_resident_memory_bytes,
// process_threads).
func registerProcessMetrics(meter otelmetric.Meter) {
	_, _ = meter.Float64ObservableCounter("process_cpu_seconds_total",
		otelmetric.WithDescription("Total user and system CPU time spent in seconds"),
		otelmetric.WithFloat64Callback(func(_ context.Context, o otelmetric.Float64Observer) error {
			if secs, err := readCPUSeconds(); err == nil {
				o.Observe(secs)
			}
			return nil
		}),
	)
	_, _ = meter.Int64ObservableGauge("process_resident_memory_bytes",
		otelmetric.WithDescription("Resident memory size in bytes"),
		otelmetric.WithInt64Callback(func(_ context.Context, o otelmetric.Int64Observer) error {
			if b, err := readRSSBytes(); err == nil {
				o.Observe(b)
			}
			return nil
		}),
	)
	_, _ = meter.Int64ObservableGauge("process_threads",
		otelmetric.WithDescription("Number of active threads"),
		otelmetric.WithInt64Callback(func(_ context.Context, o otelmetric.Int64Observer) error {
			o.Observe(int64(runtime.NumGoroutine()))
			return nil
		}),
	)
}

func parseProcStat() ([]string, error) {
	data, err := os.ReadFile("/proc/self/stat")
	if err != nil {
		return nil, err
	}
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return nil, fmt.Errorf("unexpected /proc/self/stat format")
	}
	return strings.Fields(s[idx+2:]), nil
}

func readCPUSeconds() (float64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
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
	return (utime + stime) / 100.0, nil
}

func readRSSBytes() (int64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
	if len(fields) < 22 {
		return 0, fmt.Errorf("not enough fields in /proc/self/stat")
	}
	rss, err := strconv.ParseInt(fields[21], 10, 64)
	if err != nil {
		return 0, err
	}
	return rss * int64(os.Getpagesize()), nil
}
