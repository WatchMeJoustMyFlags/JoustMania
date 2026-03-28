// Package main implements the OpAMP bridge service.
//
// The bridge watches flagd for collector-level flag changes and pushes
// updated effective configuration to the OTel Collector via OpAMP.
package main

import (
	"context"
	"log"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

func main() {
	flagdHost := getEnv("FLAGD_HOST", "localhost")
	flagdPort := getEnvUint16("FLAGD_PORT", 8013)
	listenAddr := getEnv("OPAMP_LISTEN_ADDR", ":4320")
	pollInterval := getEnvDuration("FLAG_POLL_INTERVAL", 5*time.Second)

	// Initialize OpenFeature with flagd provider.
	provider := flagd.NewProvider(
		flagd.WithHost(flagdHost),
		flagd.WithPort(flagdPort),
	)
	if err := openfeature.SetProviderAndWait(provider); err != nil {
		log.Fatalf("failed to set OpenFeature provider: %v", err)
	}
	defer openfeature.Shutdown()

	client := openfeature.NewClient("opamp-bridge")

	// Create and start the bridge.
	bridge := NewBridge(client, listenAddr, pollInterval)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if err := bridge.Start(ctx); err != nil {
		log.Fatalf("failed to start bridge: %v", err)
	}

	// Health endpoint for Docker healthcheck.
	healthMux := http.NewServeMux()
	healthMux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	healthSrv := &http.Server{Addr: ":8080", Handler: healthMux}
	go func() {
		log.Printf("health endpoint on :8080")
		if err := healthSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("health server error: %v", err)
		}
	}()

	log.Printf("opamp-bridge started: flagd=%s:%d, opamp=%s, poll=%s",
		flagdHost, flagdPort, listenAddr, pollInterval)

	// Wait for shutdown signal.
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig

	log.Println("shutting down opamp-bridge")
	cancel()
	bridge.Stop()

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer shutdownCancel()
	if err := healthSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("health server shutdown error: %v", err)
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getEnvUint16(key string, fallback uint16) uint16 {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	n, err := strconv.ParseUint(v, 10, 64)
	if err != nil || n > math.MaxUint16 {
		log.Printf("invalid %s=%q, using default %d", key, v, fallback)
		return fallback
	}
	return uint16(n)
}

func getEnvDuration(key string, fallback time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return fallback
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return fallback
	}
	return d
}
