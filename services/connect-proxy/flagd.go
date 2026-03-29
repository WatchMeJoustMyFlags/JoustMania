package main

import (
	"log/slog"
	"os"
	"strconv"

	"github.com/open-feature/go-sdk/openfeature"
	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
)

// initFlagd initializes the OpenFeature flagd provider with API-level context.
func initFlagd() {
	host := getEnv("FLAGD_HOST", "flagd")
	port := 8013
	if portStr := os.Getenv("FLAGD_PORT"); portStr != "" {
		if p, err := strconv.Atoi(portStr); err == nil {
			port = p
		} else {
			slog.Warn("Invalid FLAGD_PORT, using default", "value", portStr, "default", port)
		}
	}

	provider, providerErr := flagd.NewProvider(
		flagd.WithHost(host),
		flagd.WithPort(uint16(port)),
	)
	if providerErr != nil {
		slog.Warn("Failed to create flagd provider — flags will use defaults", "error", providerErr)
		return
	}

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
