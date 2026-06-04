// JoustMania Agent Service
//
// The agent is the front door of the agentic control stack (issue #723). It runs
// an OTLP gRPC receiver that ingests both spans and metrics emitted by the game
// services, accumulates them into a denormalized GameContext, gates on whether a
// game is live with fresh player data, and on each gated update runs a decision
// loop. The rules engine and action sink are stubbed in this scaffold; later
// issues replace them with real interventions written to a flagd domain.
//
// Self-observability (exporting the agent's own telemetry) follows the house
// pattern in otel.go and is a no-op until OTEL_EXPORTER_OTLP_ENDPOINT is set.
package main

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"google.golang.org/grpc"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
)

// Tunable lifecycle constants.
const (
	playerTTL    = 5 * time.Second
	sessionGrace = 15 * time.Second
	evictEvery   = 1 * time.Second
)

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

// parseLevel maps a LOG_LEVEL string to an slog.Level (default info).
func parseLevel(s string) slog.Level {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "debug":
		return slog.LevelDebug
	case "warn", "warning":
		return slog.LevelWarn
	case "error":
		return slog.LevelError
	default:
		return slog.LevelInfo
	}
}

func main() {
	listenAddr := getEnv("AGENT_LISTEN_ADDR", ":4317")
	healthAddr := getEnv("AGENT_HEALTH_ADDR", ":13134")
	level := parseLevel(getEnv("LOG_LEVEL", "info"))

	logger := slog.New(slog.NewTextHandler(os.Stdout, &slog.HandlerOptions{Level: level}))
	slog.SetDefault(logger)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	shutdownTracing := initTracing(ctx)
	defer shutdownTracing(context.Background())
	shutdownMetrics := initMetrics(ctx)
	defer shutdownMetrics(context.Background())

	store := gamecontext.NewStore(playerTTL, sessionGrace, nil)
	loop := decision.NewLoop(logger)
	pipe := newPipeline(store, loop, playerTTL)

	grpcServer := grpc.NewServer()
	ptraceotlp.RegisterGRPCServer(grpcServer, &traceReceiver{pipe: pipe})
	pmetricotlp.RegisterGRPCServer(grpcServer, &metricsReceiver{pipe: pipe})

	lis, err := net.Listen("tcp", listenAddr)
	if err != nil {
		slog.Error("Failed to listen", "addr", listenAddr, "error", err)
		os.Exit(1)
	}

	// Health server.
	healthMux := http.NewServeMux()
	healthMux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("OK"))
	})
	healthServer := &http.Server{Addr: healthAddr, Handler: healthMux}
	go func() {
		slog.Info("Health server listening", "addr", healthAddr)
		if err := healthServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("Health server failed", "error", err)
		}
	}()

	// Eviction ticker.
	ticker := time.NewTicker(evictEvery)
	defer ticker.Stop()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				store.EvictStale()
			}
		}
	}()

	// Graceful shutdown on signal.
	go func() {
		<-ctx.Done()
		slog.Info("Shutdown requested, stopping servers...")
		grpcServer.GracefulStop()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = healthServer.Shutdown(shutdownCtx)
	}()

	slog.Info("JoustMania Agent listening (OTLP gRPC)", "addr", listenAddr)
	if err := grpcServer.Serve(lis); err != nil {
		slog.Error("gRPC server failed", "error", err)
		os.Exit(1)
	}
}
