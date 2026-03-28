package main

import (
	"context"
	"log/slog"
	"os"

	"github.com/open-feature/go-sdk/openfeature"
	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
)

// initFlagd initializes the OpenFeature flagd provider with API-level context.
func initFlagd() {
	host := getEnv("FLAGD_HOST", "flagd")
	port := 8013

	provider := flagd.NewProvider(
		flagd.WithHost(host),
		flagd.WithPort(uint16(port)),
	)

	err := openfeature.SetProviderAndWait(provider)
	if err != nil {
		// Log warning but continue — the provider reconnects in the background,
		// and polling loops will log when flag reads recover.
		slog.Warn("Failed to initialize flagd provider — flags will use defaults until reconnect", "error", err)
	}

	// Set API-level evaluation context (available to all flag evaluations)
	serviceName := getEnv("OTEL_SERVICE_NAME", "connect-proxy")
	namespace := getEnv("OTEL_SERVICE_NAMESPACE", "infrastructure")
	hostname, _ := os.Hostname()

	openfeature.SetEvaluationContext(openfeature.NewEvaluationContext(
		"", // no targeting key at API level
		map[string]interface{}{
			"service_name":      serviceName,
			"service_namespace": namespace,
			"language":          "go",
			"hostname":          hostname,
		},
	))

	slog.Info("OpenFeature flagd provider initialized", "host", host, "port", port)
}

// shutdownFlagd cleans up the OpenFeature provider.
func shutdownFlagd() {
	openfeature.Shutdown()
}

// getFlagClient returns an OpenFeature client for flag evaluation.
func getFlagClient() openfeature.IClient {
	return openfeature.NewClient("connect-proxy")
}
