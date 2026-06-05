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

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gate"
	"github.com/joustmania/agent/infracontext"
)

// probeInterval rate-limits the AGENT_PROBE_DECISIONS demo probe.
const probeInterval = 5 * time.Second

// infraControllerTTL bounds how long a controller is retained in the
// infrastructure observe path (#733) after it stops appearing in
// controller.bluetooth_health spans. Health spans arrive at ~1Hz, so 5s ≈ five
// missed windows before a controller is considered gone.
const infraControllerTTL = 5 * time.Second

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

// actionSink returns the intervention Writer when AGENT_INTERVENTIONS_ENABLED is
// true, or nil to leave the loop's default NoopActions in place (scaffold stays
// inert). Returning decision.ActionSink keeps main()'s wiring to one line and
// makes the gate unit-testable.
func actionSink(logger *slog.Logger) decision.ActionSink {
	if !strings.EqualFold(getEnv("AGENT_INTERVENTIONS_ENABLED", ""), "true") {
		return nil
	}
	logger.Warn("Agent intervention writes enabled (#730)",
		"path", getEnv("INTERVENTIONS_FLAG_PATH", actions.DefaultPath))
	return actions.NewWriterFromEnv(logger)
}

// rolloutActuator returns the rollout write seam for the infra loop (#734). When
// AGENT_ROLLOUT_ENABLED=true it is the real RolloutWriter (flips
// current_controller_count in ROLLOUT_FLAG_PATH); otherwise it is a dry-run
// actuator that decides+spans expansions but never writes the file. Mirrors the
// actionSink env-gate shape, but always returns a non-nil actuator so the loop
// reports disabled expansions as decided-but-not-applied.
func rolloutActuator(logger *slog.Logger) decision.RolloutActuator {
	if strings.EqualFold(getEnv("AGENT_ROLLOUT_ENABLED", ""), "true") {
		logger.Warn("Agent rollout expansion enabled (#734)",
			"path", getEnv("ROLLOUT_FLAG_PATH", actions.DefaultRolloutPath))
		return actions.NewRolloutWriterFromEnv(logger)
	}
	logger.Info("Agent rollout expansion disabled (dry-run; decisions spanned, not applied)")
	return actions.NewDryRunRolloutWriter(logger)
}

// rolloutDwell reads the per-stage dwell from AGENT_ROLLOUT_DWELL_SECONDS,
// falling back to decision.DefaultRolloutDwell.
func rolloutDwell() time.Duration {
	return secondsEnv("AGENT_ROLLOUT_DWELL_SECONDS", decision.DefaultRolloutDwell)
}

// rolloutCooldown reads the post-rollback cooldown from
// AGENT_ROLLOUT_COOLDOWN_SECONDS, falling back to decision.DefaultRolloutCooldown.
func rolloutCooldown() time.Duration {
	return secondsEnv("AGENT_ROLLOUT_COOLDOWN_SECONDS", decision.DefaultRolloutCooldown)
}

// secondsEnv parses a positive integer "seconds" env var into a Duration,
// returning fallback on an empty/invalid/non-positive value.
func secondsEnv(key string, fallback time.Duration) time.Duration {
	s := strings.TrimSpace(os.Getenv(key))
	if s == "" {
		return fallback
	}
	n, err := strconv.Atoi(s)
	if err != nil || n <= 0 {
		return fallback
	}
	return time.Duration(n) * time.Second
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

	// Lifecycle + throttle calibration flags (#766 F5) are READ ONCE here, after
	// the provider is registered. They configure the store TTLs, eviction ticker,
	// and decision-loop throttle, all fixed at construction — so changing them
	// requires an agent restart (deliberately NOT hot-reload). If flagd is not yet
	// ready every value falls back to its safe default (the former hardcoded
	// constants), so the agent still starts behavior-neutrally.
	lifecycle := agentFlags.Lifecycle(ctx)
	slog.Info("Agent lifecycle flags evaluated (read-at-startup; restart to change)",
		"player_ttl", lifecycle.PlayerTTL,
		"session_grace", lifecycle.SessionGrace,
		"evict_interval", lifecycle.EvictInterval,
		"decision_throttle", lifecycle.DecisionThrottle,
	)

	store := gamecontext.NewStore(lifecycle.PlayerTTL, lifecycle.SessionGrace, nil)
	// Skip the agent's own telemetry when the collector fans it back to us
	// (otlp/agent exporter) — breaks the self-ingestion feedback loop.
	store.SetOwnService(resolveServiceName())

	// The decision loop is driven by the OpenFeature/flagd control layer (#727):
	// flags are evaluated every cycle, the kill switch / objectives / permission
	// gate are applied, and decisions flow through the #724 audit spans.
	loop := decision.NewLoop(agentFlags, logger)
	// Throttle for the evaluate log line / agent.disabled span is read-at-startup
	// from decision.throttle_seconds (#766 F5).
	loop.SetThrottle(lifecycle.DecisionThrottle)
	// The objective-weighted rules engine (#726) is the active default. Its
	// objective weights are driven live from the `objectives` flag each cycle
	// (NewObjectiveRulesLive publishes a LiveObjectives source the loop updates);
	// policy/fitness run on flagd-schema defaults. The action sink is still a
	// no-op until the intervention API (#730), so nothing is applied to the game.
	loop.Rules = decision.NewObjectiveRulesLive(nil)
	// Action sink (#730): when AGENT_INTERVENTIONS_ENABLED=true, decisions are
	// applied by rewriting the flagd interventions file (INTERVENTIONS_FLAG_PATH).
	// Default false keeps the scaffold inert (NoopActions discards decisions).
	if sink := actionSink(logger); sink != nil {
		loop.Actions = sink
	}
	if strings.EqualFold(getEnv("AGENT_PROBE_DECISIONS", ""), "true") {
		// Demo/verification mode: emit a synthetic noop decision (and thus the
		// full audit trace, #724) at most once per probe interval. Overrides
		// the rules engine.
		loop.Rules = decision.NewProbeRules(probeInterval, nil)
		slog.Warn("Probe decisions enabled (demo/verification mode)",
			"interval", probeInterval)
	}
	// Infrastructure observe + rollout path (#733/#734, M3): a parallel store fed
	// by the controller.bluetooth_health span on the same OTLP trace receiver. It
	// honors the same self-ingestion skip as the game store. The InfraLoop runs the
	// progressive-rollout expansion controller (#734): when Bluetooth fitness passes
	// and the per-stage dwell has elapsed it advances current_controller_count one
	// stage up the ladder, emitting an agent.infrastructure.decision span every
	// active-rollout cycle.
	infraStore := infracontext.NewStore(infraControllerTTL, nil)
	infraStore.SetOwnService(resolveServiceName())
	// Bluetooth fitness source (#735): seeds the flagd-schema defaults; the infra
	// loop reads the live fitness.bluetooth.* thresholds through it each cycle.
	bluetoothFitness := decision.NewLiveBluetoothFitness()
	// Rollout actuator (#734): real RolloutWriter when AGENT_ROLLOUT_ENABLED=true,
	// else a dry-run actuator (decides+spans but does not write rollout.json).
	infraLoop := decision.NewInfraLoop(logger, rolloutDwell(), bluetoothFitness, rolloutActuator(logger))
	// Auto-remediation gate (#736): remediation_allowed lives in the ROLLOUT flagd
	// domain (rollout.json), so the loop reads it directly from that file (the agent
	// already owns the path). When false, fitness failures are RECOMMENDED only (span,
	// no write). Default on any read error is false (fail-closed). The dry-run
	// actuator above also applies to rollback, so a disabled rollout records the
	// rollback decision without writing.
	infraLoop.SetRemediationSource(actions.NewRemediationReaderFromEnv(logger))
	// Post-rollback cooldown (#736): suppress re-expansion for this long after a
	// rollback so fitness can recover before the loop climbs again.
	infraLoop.SetCooldown(rolloutCooldown())
	// Freshness gate (#734): evaluate the rollout only when ≥1 controller is
	// reporting fresh Bluetooth health. Game-state-independent (lobby connects).
	infraLoop.SetGate(func(snap infracontext.InfraContext, now time.Time) bool {
		return gate.ShouldEvaluateInfra(snap, now, infraControllerTTL)
	})
	pipe := newPipeline(store, loop, lifecycle.PlayerTTL).withInfra(infraStore, infraLoop)

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
	ticker := time.NewTicker(lifecycle.EvictInterval)
	defer ticker.Stop()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				store.EvictStale()
				infraStore.EvictStale()
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
