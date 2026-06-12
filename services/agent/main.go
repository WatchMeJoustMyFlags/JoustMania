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

	"github.com/google/uuid"

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
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

// partitionSet turns the Multiplexer's partition id slice into a set for
// LoopSet.Retain (#845 PR C).
func partitionSet(ids []string) map[string]struct{} {
	set := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		set[id] = struct{}{}
	}
	return set
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

// runShadowGame executes the env-triggered shadow game (#778): it sweeps
// orphaned reserved controllers (the "agent:" tag prefix) left by a prior
// crashed run, then runs one mock-only game to completion. Errors are logged,
// not fatal — the shadow game is an auxiliary capability and must never take the
// agent's observation path down.
func runShadowGame(ctx context.Context, logger *slog.Logger) {
	runID := uuid.NewString()
	cfg := gamerunner.ConfigFromEnv()
	spec := gamerunner.SpecFromEnv(runID)
	runner := gamerunner.New(cfg, logger)

	logger.Info("Shadow-game runner enabled (#778)",
		"coordinator", cfg.CoordinatorAddr, "mock", cfg.MockAddr, "spec", spec.String())

	if sweep, err := runner.SweepOrphans(ctx, "agent:"); err != nil {
		logger.Warn("Shadow-game orphan sweep failed (continuing)", "error", err)
	} else if len(sweep.Removed) > 0 {
		logger.Info("Shadow-game orphan sweep removed controllers", "count", len(sweep.Removed))
	}

	result, err := runner.RunShadowGame(ctx, spec)
	if err != nil {
		logger.Error("Shadow game failed", "run_id", runID, "game_id", result.GameID, "error", err)
		return
	}
	logger.Info("Shadow game finished",
		"run_id", runID,
		"game_id", result.GameID,
		"outcome", result.Outcome,
		"terminal_event", result.TerminalEvent,
		"events_seen", result.EventsSeen,
		"duration", result.Duration,
	)
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
	agentFlags, lifecycleHolder, shutdownFlags, err := flags.SetupFlagd(ctx, flagCfg, logger)
	if err != nil {
		slog.Error("Failed to set up flagd provider", "error", err)
		os.Exit(1)
	}
	defer shutdownFlags()
	slog.Info("OpenFeature flagd provider registered", "host", flagCfg.Host, "port", flagCfg.Port)

	// Lifecycle + throttle calibration flags (#766 F5) are HOT-RELOADED live since
	// #927: SetupFlagd primed the holder with the four flags and wired the
	// OpenFeature configuration-change listener (the maintainer's requested
	// mechanism), so the store TTLs, eviction ticker, and decision-loop throttle
	// read the CURRENT values from the holder on their hot path and a flag change
	// takes effect with no restart. The initial snapshot below is only for the
	// startup log; the consumers wired further down read the holder's live source
	// funcs, never this captured value. If flagd is not yet ready every value falls
	// back to its safe default (the former hardcoded constants).
	lifecycle := lifecycleHolder.Load()
	slog.Info("Agent lifecycle flags primed (hot-reloaded live via OpenFeature config-change, #927)",
		"player_ttl", lifecycle.PlayerTTL,
		"session_grace", lifecycle.SessionGrace,
		"evict_interval", lifecycle.EvictInterval,
		"decision_throttle", lifecycle.DecisionThrottle,
	)

	ownService := resolveServiceName()

	// Per-game decision Loop factory (#845 PR C). Each game partition gets its OWN
	// Loop — own weighted rate-limit budget, own log/span throttle, own per-cycle
	// LayerState — so concurrent games never share a budget or contend on throttle.
	// The LoopSet below invokes this once per game_id, lazily on first evaluation.
	//
	// Shared vs per-game wiring:
	//   - FRESH rules engine PER LOOP: NewObjectiveRulesLive builds a new engine with
	//     its own LiveObjectives / LiveFitness sources. The loop drives these every
	//     cycle (SetObjectives/SetFitness before Evaluate, LastFitness after), so a
	//     shared engine would let two games race each other's objective weights and
	//     fitness reads — the spans would carry the wrong game's agent.objectives.
	//     A fresh engine removes the shared mutable state entirely.
	//   - SHARED action sink: actions.Writer holds no per-game state (it serializes a
	//     read-modify-write of the flagd interventions file under its own mutex,
	//     keyed by the decision's target), so one Writer is closed over by every loop.
	//   - SHARED flag source (agentFlags) and tracer: stateless / concurrency-safe.
	sharedSink := actionSink(logger) // nil leaves the loop's default NoopActions in place
	// Shared GLOBAL LLM request budget (#847, the gate's third layer). Constructed
	// ONCE here and injected into every per-game Loop below, so all concurrent
	// games draw down a SINGLE per-minute request cap (llm.max_requests_per_minute)
	// — 4 idle shadow games plus a real game can never collectively exceed it. The
	// per-game cadence + eligibility layers live on each Loop; only the budget is
	// shared across loops.
	sharedLLMBudget := decision.NewLLMBudget()
	// Startup self-heal (#924): when the real action sink is enabled, validate the
	// interventions file and repair a poisoned (unparseable) one to the neutral
	// document before the first decision cycle. A corrupt file would otherwise make
	// flagd reject the whole flag set, silently defaulting every intervention. The
	// heal is best-effort: a failure logs and the agent still starts.
	if writer, ok := sharedSink.(*actions.Writer); ok {
		if healed, err := writer.HealIfCorrupt(ctx); err != nil {
			slog.Error("Interventions file startup self-heal failed (continuing)", "error", err)
		} else if healed {
			slog.Warn("Interventions file was corrupt at startup; restored neutral document (#924)")
		}
	}
	// Shared inference fallback-chain resolver (#741). Constructed ONCE here with the
	// real network probes for the three llm tiers (cloud #742, gemma3:4b on Jetson
	// #738, phi4-mini on localhost #739) and ONE availability cache for the whole
	// agent, then injected into every per-game Loop below — like the global budget.
	// In dev every endpoint is unreachable, so resolve_backend honestly degrades the
	// whole chain to the rules engine; with a tier reachable it reports that tier as
	// inference.used (who #739 WILL call). Start() launches the background re-probe
	// ticker (DefaultProbeInterval) and primes the cache in the background; it
	// stops when ctx is cancelled at shutdown.
	sharedResolver := decision.NewResolver(decision.DefaultChain(decision.Endpoints{
		Cloud:     getEnv("AGENT_CLOUD_ENDPOINT", ""),                    // #742 credential-blocked: empty = permanently unreachable
		Jetson:    getEnv("AGENT_JETSON_ENDPOINT", "jetson:11434"),       // #738 Ollama on the Jetson; unresolvable in dev
		Localhost: getEnv("AGENT_LOCALHOST_ENDPOINT", "localhost:11434"), // #739 Ollama locally; unreachable unless running
	}), decision.DefaultProbeInterval)
	sharedResolver.Start(ctx)
	probeDecisions := strings.EqualFold(getEnv("AGENT_PROBE_DECISIONS", ""), "true")
	if probeDecisions {
		slog.Warn("Probe decisions enabled (demo/verification mode)",
			"interval", probeInterval)
	}
	loopFactory := func(gameID string) *decision.Loop {
		// The decision loop is driven by the OpenFeature/flagd control layer (#727):
		// flags are evaluated every cycle, the kill switch / objectives / permission
		// gate are applied, and decisions flow through the #724 audit spans.
		loop := decision.NewLoop(agentFlags, logger)
		// Throttle for the evaluate log line / agent.disabled span reads
		// decision.throttle_seconds LIVE from the shared LifecycleHolder (#927): a
		// config-change to the throttle flag is honored on the next cycle with no
		// restart (the holder is a lock-free atomic load, so the hot path never
		// evaluates flagd). Every per-game loop shares the one holder source.
		loop.SetThrottleSource(lifecycleHolder.DecisionThrottle)
		// Inject the SHARED global LLM request budget (#847) so every per-game loop
		// references the same instance: the gate's eligibility + cadence layers are
		// per-loop, but the budget layer is one global cap across all games.
		loop.SetLLMBudget(sharedLLMBudget)
		// Inject the SHARED inference resolver (#741): every per-game loop resolves the
		// fallback chain against the same availability cache, so a gate-admitted llm
		// cycle reports the honest inference.used tier and any degradation reason.
		loop.SetResolver(sharedResolver)
		// The objective-weighted rules engine (#726) is the active default, built
		// FRESH per loop (see the factory doc above). Its objective weights are driven
		// live from the `objectives` flag each cycle; policy/fitness run on
		// flagd-schema defaults.
		loop.Rules = decision.NewObjectiveRulesLive(nil)
		if probeDecisions {
			// Demo/verification mode: emit a synthetic noop decision (and thus the
			// full audit trace, #724) at most once per probe interval. Overrides
			// the rules engine. Each loop gets its own probe clock.
			loop.Rules = decision.NewProbeRules(probeInterval, nil)
		}
		// Action sink (#730): when AGENT_INTERVENTIONS_ENABLED=true, decisions are
		// applied by rewriting the flagd interventions file (INTERVENTIONS_FLAG_PATH).
		// Default (nil sink) keeps the scaffold inert (NoopActions discards decisions).
		// The one Writer is safe to share across loops (no per-game state).
		if sharedSink != nil {
			loop.Actions = sharedSink
		}
		return loop
	}
	loops := decision.NewLoopSet(loopFactory)
	// Infrastructure observe + rollout path (#733/#734, M3): a parallel store fed
	// by the controller.bluetooth_health span on the same OTLP trace receiver. It
	// honors the same self-ingestion skip as the game store. The InfraLoop runs the
	// progressive-rollout expansion controller (#734): when Bluetooth fitness passes
	// and the per-stage dwell has elapsed it advances current_controller_count one
	// stage up the ladder, emitting an agent.infrastructure.decision span every
	// active-rollout cycle.
	infraStore := infracontext.NewStore(infraControllerTTL, nil)
	infraStore.SetOwnService(resolveServiceName())
	// Bluetooth fitness source (#735): backed DIRECTLY by the flags client, so the
	// infra loop re-reads the live fitness.bluetooth.* thresholds on every observe
	// cycle (~1Hz) — tunable live on stage with no restart, and in the lobby too
	// (the game loop only publishes thresholds while a session is active, so it
	// cannot keep the infra loop live on its own). On any evaluation error the
	// accessor falls back to the flagd-schema defaults.
	bluetoothFitness := decision.NewFlagBluetoothFitness(agentFlags)
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
	// Post-game retrospective (#844): on the GameActive true->false transition each
	// partition's store fires OnGameEnd with a pre-reset snapshot, and the
	// RetroCoordinator captures the prompt the agent would send to an offline
	// analyst on a dedicated agent.llm.retro span (capture-first, exactly once per
	// session). It deliberately does NOT touch the loop, the gate, or the rate
	// limiter — a retrospective never consumes the in-game intervention budget. With
	// per-game partitions (#845 PR B) every partition's Store shares this one
	// coordinator; it dedupes by SessionID across all partitions, so interleaved
	// game-ends each capture exactly once (see retro_capture.go's bounded dedupe).
	retro := decision.NewRetroCoordinator(agentFlags, logger)
	// A retrospective is one LLM request, so it draws from the SAME shared global
	// budget the in-game gate uses (#847 acceptance #7): inject the one instance the
	// per-game loops also hold, so a retro at game end cannot blow the per-minute cap.
	retro.SetLLMBudget(sharedLLMBudget)

	// GameContext multiplexer (#845 PR B): one Store partition per game_id, plus the
	// fallback partition "" for unlabeled signals (zero-regression — single-game
	// mode collapses to exactly today's single-store behavior). The factory applies
	// the lifecycle TTLs/clock, skips the agent's own telemetry (otlp/agent
	// self-ingestion loop), and wires OnGameEnd per partition so the retrospective
	// fires per game on the right partition's state.
	mux := gamecontext.NewMultiplexer(func(gameID string) *gamecontext.Store {
		// Prime with the holder's current values as the static fallback, then wire the
		// LIVE TTL sources (#927): EvictStale reads lifecycle.player_ttl_seconds /
		// lifecycle.session_grace_seconds from the shared holder at eviction time, so a
		// config-change is honored on the next tick with no restart. Every partition
		// created later shares the same holder source funcs.
		s := gamecontext.NewStore(lifecycle.PlayerTTL, lifecycle.SessionGrace, nil)
		s.SetTTLSources(lifecycleHolder.PlayerTTL, lifecycleHolder.SessionGrace)
		s.SetOwnService(ownService)
		s.OnGameEnd = retro.OnGameEnd
		return s
	})
	mux.SetOwnService(ownService)

	pipe := newPipeline(mux, loops, lifecycle.PlayerTTL).
		// Hot-reload the evaluate gate's freshness window in lockstep with the store
		// eviction TTL (#927): both read lifecycle.player_ttl_seconds from the holder.
		withPlayerTTLSource(lifecycleHolder.PlayerTTL).
		withInfra(infraStore, infraLoop)

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

	// Eviction ticker. The interval is HOT-RELOADED from the holder (#927): after
	// each fire we re-read lifecycle.evict_interval_seconds and, if it changed,
	// Reset the ticker so a config-change to the eviction cadence takes effect with
	// no restart. The holder read is a lock-free atomic load, so this costs nothing
	// on the steady-state path; we only call Reset when the value actually moved.
	go func() {
		interval := lifecycleHolder.EvictInterval()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				mux.EvictStale()
				// Drop the Loop (budget + throttle state) of any partition the
				// Multiplexer just removed, in lockstep with its Store (#845 PR C).
				// The fallback partition's loop is never dropped (Retain skips it).
				// Partitions() is read AFTER EvictStale so a removed game's loop is
				// not retained; it is recreated lazily (fresh budget) if it resumes.
				loops.Retain(partitionSet(mux.Partitions()))
				infraStore.EvictStale()
				// Re-arm at the current configured interval if it changed (#927).
				if next := lifecycleHolder.EvictInterval(); next > 0 && next != interval {
					interval = next
					ticker.Reset(interval)
				}
			}
		}
	}()

	// Shadow-game runner (#778): when AGENT_SHADOW_GAME=true, sweep any orphaned
	// reserved controllers from a prior crashed run, then run ONE mock-only game
	// to completion in the background. The OTLP receiver below keeps serving; the
	// run does not block startup. The default (unset) leaves the agent inert.
	if gamerunner.Enabled() {
		go runShadowGame(ctx, logger)
	}

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
