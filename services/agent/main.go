// JoustMania Agent Service
//
// The agent is the front door of the agentic control stack (issue #723). It runs
// an OTLP gRPC receiver that ingests both spans and metrics emitted by the game
// services, accumulates them into a denormalized GameContext, gates on whether a
// game is live with fresh player data, and on each gated update runs a decision
// loop. The objective-weighted rules engine (#726) produces decisions; the
// action sink is stubbed until the intervention API (#730) writes them to a
// flagd domain.
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
	"strconv"
	"strings"
	"syscall"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"google.golang.org/grpc"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// Tunable lifecycle constants.
const (
	playerTTL    = 5 * time.Second
	sessionGrace = 15 * time.Second
	evictEvery   = 1 * time.Second
	// probeInterval rate-limits the AGENT_PROBE_DECISIONS demo probe.
	probeInterval = 5 * time.Second
)

// defaultServiceName is the agent's OTEL service name default. It MUST stay
// identical everywhere it is used: the exported resource (otel.go) and the
// extractor self-skip (SetOwnService) rely on matching values to break the
// otlp/agent self-ingestion loop.
const defaultServiceName = "agent"

// resolveServiceName resolves the agent's OTEL service name (env override or default).
func resolveServiceName() string {
	return getEnv("OTEL_SERVICE_NAME", defaultServiceName)
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

// parsePort parses a uint16 TCP port, returning fallback on any parse error.
func parsePort(s string, fallback uint16) uint16 {
	n, err := strconv.ParseUint(strings.TrimSpace(s), 10, 16)
	if err != nil {
		return fallback
	}
	return uint16(n)
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

	// OpenFeature / flagd control layer. Setup is non-blocking: if flagd is
	// unreachable the provider connects in the background and every evaluation
	// falls back to its safe default (enabled=false), so the agent still starts.
	flagCfg := flags.ProviderConfig{
		Host: getEnv("FLAGD_HOST", "flagd"),
		Port: parsePort(getEnv("FLAGD_PORT", "8013"), 8013),
	}
	agentFlags, shutdownFlags, err := flags.SetupFlagd(flagCfg, logger)
	if err != nil {
		slog.Error("Failed to set up flagd provider", "error", err)
		os.Exit(1)
	}
	defer shutdownFlags()
	slog.Info("OpenFeature flagd provider registered", "host", flagCfg.Host, "port", flagCfg.Port)

	store := gamecontext.NewStore(playerTTL, sessionGrace, nil)
	// Skip the agent's own telemetry when the collector fans it back to us
	// (otlp/agent exporter) — breaks the self-ingestion feedback loop.
	store.SetOwnService(resolveServiceName())

	// The decision loop is driven by the OpenFeature/flagd control layer (#727):
	// flags are evaluated every cycle, the kill switch / objectives / permission
	// gate are applied, and decisions flow through the #724 audit spans.
	loop := decision.NewLoop(agentFlags, logger)
	// The objective-weighted rules engine (#726) is the active default. Its
	// objective weights are driven live from the `objectives` flag each cycle
	// (NewObjectiveRulesLive publishes a LiveObjectives source the loop updates);
	// policy/fitness run on flagd-schema defaults. The action sink is still a
	// no-op until the intervention API (#730), so nothing is applied to the game.
	loop.Rules = decision.NewObjectiveRulesLive(nil)
	if strings.EqualFold(getEnv("AGENT_PROBE_DECISIONS", ""), "true") {
		// Demo/verification mode: emit a synthetic noop decision (and thus the
		// full audit trace, #724) at most once per probe interval. Overrides
		// the rules engine.
		loop.Rules = decision.NewProbeRules(probeInterval, nil)
		slog.Warn("Probe decisions enabled (demo/verification mode)",
			"interval", probeInterval)
	}
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
