package main

import (
	"context"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
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

// resolveHostName reads host.name from OTEL_RESOURCE_ATTRIBUTES if set,
// falling back to os.Hostname(). Inside Docker, os.Hostname() returns the
// container ID; OTEL_RESOURCE_ATTRIBUTES provides the real host name.
func resolveHostName() string {
	if attrs := os.Getenv("OTEL_RESOURCE_ATTRIBUTES"); attrs != "" {
		for _, pair := range strings.Split(attrs, ",") {
			if k, v, ok := strings.Cut(pair, "="); ok {
				if strings.TrimSpace(k) == "host.name" {
					return strings.TrimSpace(v)
				}
			}
		}
	}
	h, _ := os.Hostname()
	return h
}

// newOTELResource builds a shared resource with standard OTEL semantic-convention attributes.
func newOTELResource(ctx context.Context, serviceName, namespace string) (*resource.Resource, error) {
	hostname := resolveHostName()
	return resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceName(serviceName),
			semconv.ServiceNamespace(namespace),
			semconv.ServiceVersion(getEnv("SERVICE_VERSION", "0.0.0-dev")),
			semconv.ServiceInstanceID(getEnv("HOSTNAME", hostname)),
			attribute.String("deployment.environment", getEnv("DEPLOYMENT_ENVIRONMENT", "development")),
			semconv.HostName(hostname),
			semconv.ContainerName(serviceName),
			attribute.Int("process.pid", os.Getpid()),
			attribute.String("process.executable.name", defaultServiceName),
			attribute.String("process.runtime.name", "go"),
			attribute.String("process.runtime.version", runtime.Version()),
			attribute.String("telemetry.sdk.name", "opentelemetry"),
			attribute.String("telemetry.sdk.language", "go"),
		),
	)
}

// initTracing sets up OTEL trace export via OTLP and returns a shutdown function.
// When OTEL_EXPORTER_OTLP_ENDPOINT is not set, returns a no-op.
func initTracing(ctx context.Context) func(context.Context) error {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		return func(context.Context) error { return nil }
	}

	serviceName := resolveServiceName()
	namespace := getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure")

	endpoint = strings.TrimPrefix(endpoint, "http://")

	exporter, err := otlptracegrpc.New(ctx,
		otlptracegrpc.WithEndpoint(endpoint),
		otlptracegrpc.WithInsecure(),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL trace exporter: %v\n", err)
		return func(context.Context) error { return nil }
	}

	res, err := newOTELResource(ctx, serviceName, namespace)
	if err != nil {
		fmt.Printf("Failed to create OTEL resource for tracing: %v\n", err)
		return func(context.Context) error { return nil }
	}

	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(
		propagation.TraceContext{},
		propagation.Baggage{},
	))

	fmt.Println("OTEL tracing initialized")
	return provider.Shutdown
}

// initMetrics sets up OTEL metrics with process metrics pushed to the collector.
// Returns a shutdown function. If OTEL_EXPORTER_OTLP_ENDPOINT is not set, returns a no-op.
func initMetrics(ctx context.Context) func(context.Context) error {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		return func(context.Context) error { return nil }
	}

	serviceName := resolveServiceName()
	namespace := getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure")

	endpoint = strings.TrimPrefix(endpoint, "http://")

	exporter, err := otlpmetricgrpc.New(ctx,
		otlpmetricgrpc.WithEndpoint(endpoint),
		otlpmetricgrpc.WithInsecure(),
	)
	if err != nil {
		fmt.Printf("Failed to create OTEL metrics exporter: %v\n", err)
		return func(context.Context) error { return nil }
	}

	res, err := newOTELResource(ctx, serviceName, namespace)
	if err != nil {
		fmt.Printf("Failed to create OTEL resource: %v\n", err)
		return func(context.Context) error { return nil }
	}

	reader := metric.NewPeriodicReader(exporter, metric.WithInterval(1*time.Second))

	provider := metric.NewMeterProvider(
		metric.WithReader(reader),
		metric.WithResource(res),
	)

	// Publish as the global meter provider so instrumented packages (e.g.
	// actions.Writer) can obtain meters via otel.Meter(...) without threading
	// the provider through their constructors.
	otel.SetMeterProvider(provider)

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

	// process_resident_memory_bytes — from /proc/self/stat rss field
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
	s := string(data)
	idx := strings.LastIndex(s, ")")
	if idx < 0 || idx+2 >= len(s) {
		return nil, fmt.Errorf("unexpected /proc/self/stat format")
	}
	fields := strings.Fields(s[idx+2:])
	return fields, nil
}

// readCPUSeconds reads cumulative CPU seconds from /proc/self/stat.
func readCPUSeconds() (float64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
	// After comm: index 11 = utime, index 12 = stime.
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
	clkTck := 100.0 // typical Linux CLK_TCK
	return (utime + stime) / clkTck, nil
}

// readRSSBytes reads resident set size from /proc/self/stat.
func readRSSBytes() (int64, error) {
	fields, err := parseProcStat()
	if err != nil {
		return 0, err
	}
	// After comm: index 21 = rss (in pages).
	if len(fields) < 22 {
		return 0, fmt.Errorf("not enough fields in /proc/self/stat")
	}
	rss, err := strconv.ParseInt(fields[21], 10, 64)
	if err != nil {
		return 0, err
	}
	return rss * int64(os.Getpagesize()), nil
}
