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
	"encoding/json"
	"errors"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/gamewindow"
	"github.com/joustmania/agent/gate"
	"github.com/joustmania/agent/inference"
	"github.com/joustmania/agent/infracontext"
	"github.com/joustmania/agent/llm"
	"github.com/joustmania/agent/promote"
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

// effectWindowFromEnv resolves the intervention-effect follow-up window (#918) from
// AGENT_EFFECT_WINDOW_SECONDS (a float of seconds). An unset, unparseable, or
// non-positive value falls back to decision.DefaultEffectWindow so the agent always
// has a sane window.
func effectWindowFromEnv() time.Duration {
	raw := getEnv("AGENT_EFFECT_WINDOW_SECONDS", "")
	if raw == "" {
		return decision.DefaultEffectWindow
	}
	secs, err := strconv.ParseFloat(raw, 64)
	if err != nil || secs <= 0 {
		slog.Warn("AGENT_EFFECT_WINDOW_SECONDS invalid; using default (#918)",
			"value", raw, "default", decision.DefaultEffectWindow)
		return decision.DefaultEffectWindow
	}
	return time.Duration(secs * float64(time.Second))
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

// chainGameEnd composes several gamecontext.Store.OnGameEnd callbacks into one,
// invoking each in order with the same snapshot (#928). The store exposes a single
// OnGameEnd hook, but multiple independent once-per-game consumers (the #844 retro
// and the M7-1 summary builder) need it; chaining keeps both on the one hook
// without either knowing about the other. nil callbacks are skipped so a disabled
// consumer is a no-op.
func chainGameEnd(fns ...func(gamecontext.GameContext)) func(gamecontext.GameContext) {
	return func(c gamecontext.GameContext) {
		for _, fn := range fns {
			if fn != nil {
				fn(c)
			}
		}
	}
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

// buildPromoter constructs the M7-8 code-improvement Promoter (#936) with the
// SAFETY RAIL applied at the one place it can be: this is the ONLY site where the
// real, consequential clients (GitHub API, local git, real-default writer) are
// ever built, each behind its explicit env gate. Everything else (tests, the
// default path) gets the inert recording no-ops.
//
// The real clients are built ONLY when AGENT_CODE_IMPROVEMENT_ENABLED=true AND the
// per-target conditions hold (GitHub: GITHUB_TOKEN + GITHUB_REPOSITORY + the live
// target=github flag; local: AGENT_LOCAL_REPO_DIR; autonomous real-default:
// AGENT_AUTONOMOUS_ENABLED + AGENT_REAL_DEFAULT_FLAG_PATH). Absent any condition,
// the corresponding FromEnv constructor returns nil and NewPromoter substitutes the
// inert no-op / leaves autonomous as a recorded no-op — fail-closed, never a silent
// real action. The promotion mode/target are read LIVE per promotion (flags), so
// flipping the mode flag takes effect on the next promotion with no restart.
//
// The default flag values are the SAFE ones (mode=issue, target=local; see
// services/flagd/agent.json + flags.DefaultPromotion*), so even a fully-enabled
// agent only opens an issue until an operator deliberately flips the flags.
//
// NOTE on the live trigger: the propose→validate→PROMOTE pipeline that CALLS
// Promote on a validated experiment is the documented follow-up (the proposer is
// #931, parallel; the SyntheticRunner/Watcher live wiring reuses M7-7's seams where
// merged). buildPromoter wires the env-gated promotion SURFACE; the autonomous
// watch+revert seams (Watcher/RealDefaultReverter) are nil until the live real-game
// fitness source (promote.RealGameFitness, reading the #731 evaluator / gamewindow)
// is wired.
//
// FAIL-CLOSED (#1016/#1057): the autonomous Watcher + RealDefaultReverter are now
// LIVE-WIRED, but only when autonomous is safely enabled (the same env gate the
// real-default writer uses) AND a real fitness source is available. When the gate is
// off — the default — the Watcher stays nil, so autonomous REFUSES to apply (a hard
// stop), NOT an unguarded real-default change. The live wiring:
//   - RealGameFitness: reads recent REAL-game fitness from the #965 FitnessStore (the
//     same finalized-per-game real fitness the recent-real baseline reads). Until real
//     games populate the store it returns 0 samples ⇒ INSUFFICIENT ⇒ apply stays
//     unverified + re-watched, never a false healthy keep.
//   - RealDefaultReverter: the SAME concrete fileRealDefaultWriter, which snapshots the
//     pre-promotion {defaultVariant + value} at write time and restores it exactly on
//     revert (#1065).
//   - Rewatcher: a background poller that re-observes an insufficient-games apply as
//     more real games complete (#1065 item 4).
//   - Cooldown: a post-revert cooldown suppresses re-promotion thrash (#1065 item 3).
func buildPromoter(rootCtx context.Context, store *experiment.FitnessStore, objResolver *promote.LateObjectiveResolver, logger *slog.Logger) *promote.Promoter {
	// Resolve the target for client construction from the ENV (AGENT_CODE_IMPROVEMENT_TARGET),
	// distinct from the LIVE flag (code_improvement.target) that Promote dispatches on.
	// This is a deliberate DOUBLE gate and is STRICTER than a single switch: opening a
	// real GitHub PR needs BOTH the env target=github at construction (so the http
	// client is even built) AND the live flag target=github at dispatch. If they
	// diverge (env=local, flag=github) the dispatch hits the inert no-op and degrades
	// to a RECORDED no-op — safe, but an operator who flips only the live flag will see
	// recorded no-ops; flipping to real GitHub requires the env target too.
	target := promote.Target(getEnv("AGENT_CODE_IMPROVEMENT_TARGET", string(promote.TargetLocal)))

	github, err := promote.NewGitHubClientFromEnv(target, logger)
	if err != nil {
		logger.Error("code_improvement GitHub client misconfigured; using inert no-op", "error", err)
		github = nil // NewPromoter substitutes the recording no-op
	}
	git, err := promote.NewGitClientFromEnv(target, logger)
	if err != nil {
		logger.Error("code_improvement local git client misconfigured; using inert no-op", "error", err)
		git = nil
	}
	realDef, err := promote.NewRealDefaultWriterFromEnv(logger)
	if err != nil {
		logger.Error("code_improvement real-default writer misconfigured; autonomous stays a no-op", "error", err)
		realDef = nil
	}

	// FromEnv returns a typed-nil *concrete pointer when ungated; pass nil interfaces
	// to NewPromoter so its nil checks substitute the no-ops (a typed-nil in an
	// interface is non-nil and would defeat them).
	var ghIface promote.GitHubClient
	if github != nil {
		ghIface = github
	}
	var gitIface promote.GitClient
	if git != nil {
		gitIface = git
	}
	// The real-default writer is ALSO the reverter (it snapshots prior state at write
	// time, #1065). Keep a concrete handle so the same instance backs both seams.
	var realDefIface promote.RealDefaultWriter
	var reverterIface promote.RealDefaultReverter
	if realDef != nil {
		realDefIface = realDef
		reverterIface = realDef
	}

	repo := promote.RepoRef{
		Owner: firstField(getEnv("GITHUB_REPOSITORY", ""), 0),
		Name:  firstField(getEnv("GITHUB_REPOSITORY", ""), 1),
		Base:  getEnv("GITHUB_BASE_BRANCH", "main"),
	}

	// LIVE WATCH (#1057/#1065): build the Watcher over the real fitness source ONLY
	// when the real-default writer is present (the autonomous env gate is satisfied AND
	// a real-default path exists) AND a FitnessStore is available to read real-game
	// fitness from. Otherwise leave the Watcher nil so autonomous FAILS CLOSED (refuses
	// to apply) — never an unguarded real-default change.
	var watcherIface promote.Watcher
	var rewatcher promote.Rewatcher
	if realDef != nil && store != nil {
		fitnessSource := promote.NewStoreRealGameFitness(store, objResolver.Resolve)
		watcher := promote.NewFitnessWatcher(fitnessSource, promote.DefaultWatchPolicy(), logger)
		watcherIface = watcher
		rewatcher = promote.NewPollingRewatcher(rootCtx, rewatchInterval(), rewatchTimeout(), logger)
		logger.Warn("code_improvement AUTONOMOUS watch+revert LIVE-WIRED (#1057/#1065): autonomous promotions are now a closed apply→watch→keep-or-revert loop")
	} else {
		logger.Info("code_improvement autonomous watch NOT wired (autonomous env gate off or no fitness store); autonomous fails closed (#1016)")
	}

	p := promote.NewPromoter(ghIface, gitIface, realDefIface, watcherIface, reverterIface, repo, nil, logger)
	p.SetCooldown(revertCooldown())
	// Seed the post-revert cooldown from the durable snapshot store (#1076 item 3) so
	// thrash protection survives an agent restart. realDef carries the persisted revert
	// timestamps; a nil writer (autonomous off) yields nothing to seed.
	if realDef != nil {
		p.SeedLastRevert(realDef.PersistedRevertTimes())
	}
	if rewatcher != nil {
		p.SetRewatcher(rewatcher)
	}
	return p
}

// firstField splits an "owner/repo" string and returns the owner (i==0) or repo
// (i==1), or "" when absent.
func firstField(repo string, i int) string {
	parts := strings.SplitN(repo, "/", 2)
	if i < len(parts) {
		return parts[i]
	}
	return ""
}

// objectiveForFlag resolves the fitness objective a promoted flag's experiment
// serves by scanning the registry's LIVE experiments for one whose intent targets
// flagKey (#1057). The autonomous Watcher uses this to read the right per-objective
// recent-real fitness from the FitnessStore. Best-effort: a concluded/torn-down
// experiment is gone from Live(), so this returns "" (any-objective) — a safe
// degrade, since the watcher still observes a recent-real fitness trend, just
// unfiltered by objective.
func objectiveForFlag(reg *experiment.Registry, flagKey string) string {
	if reg == nil || flagKey == "" {
		return ""
	}
	for _, id := range reg.Live() {
		if cv, ok := reg.CompactView(id); ok && cv.Intent.FlagKey == flagKey {
			return cv.Intent.Objective
		}
	}
	return ""
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

// llmBudgetOverride reads the LIVE LLM per-minute request cap override from
// AGENT_LLM_MAX_REQUESTS_PER_MINUTE (#964). The decision loop normally reads the cap
// from the llm.max_requests_per_minute flag (default 6), which under the eval-cycle
// rate exhausts the budget almost immediately and gates the llm path with
// llm_budget_exhausted even when a backend is reachable. This env override lets an
// operator raise the cap for a real-inference dry-run without editing flagd. An
// unset/empty/invalid/non-positive value returns 0 = "no override" (the flag
// governs), so the default-off path is unchanged.
func llmBudgetOverride() int {
	s := strings.TrimSpace(os.Getenv("AGENT_LLM_MAX_REQUESTS_PER_MINUTE"))
	if s == "" {
		return 0
	}
	n, err := strconv.Atoi(s)
	if err != nil || n <= 0 {
		slog.Warn("AGENT_LLM_MAX_REQUESTS_PER_MINUTE invalid; ignoring (flag governs) (#964)", "value", s)
		return 0
	}
	return n
}

// revertCooldown reads the autonomous post-revert re-promotion cooldown (#1065
// item 3) from AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS, else the package default.
func revertCooldown() time.Duration {
	return secondsEnv("AGENT_AUTONOMOUS_REVERT_COOLDOWN_SECONDS", promote.DefaultRevertCooldown)
}

// rewatchInterval reads the background re-watch poll interval (#1065 item 4) from
// AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS, else the package default.
func rewatchInterval() time.Duration {
	return secondsEnv("AGENT_AUTONOMOUS_REWATCH_INTERVAL_SECONDS", promote.DefaultRewatchInterval)
}

// rewatchTimeout reads how long a background re-watch waits for enough real games
// (#1065 item 4) from AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS, else the default.
func rewatchTimeout() time.Duration {
	return secondsEnv("AGENT_AUTONOMOUS_REWATCH_TIMEOUT_SECONDS", promote.DefaultRewatchTimeout)
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

// inferFunc is the shared inference seam shape (#1046/#1048): Infer(ctx, prompt) →
// raw text. Both experiment.Backend (the proposer) and decision.Backend (the
// chain) require exactly this method, so ONE value satisfies both. selectInferenceBackend
// returns it (nil = the stub default) so main.go feeds the same backend to both seams.
type inferFunc = func(ctx context.Context, prompt llm.Prompt) (string, error)

// selectInferenceBackend reads the AGENT_INFERENCE_* env config (#1048) and returns
// the inference delegate to feed BOTH the proposer and the decision chain — or nil
// when the stub backend is selected (the DEFAULT → no behavior change: the proposer
// keeps the inert proposeBackend{} stub and the decision chain keeps the
// not-implemented sentinel, both degrading safely as before).
//
// AGENT_INFERENCE_BACKEND: "stub" (default) | "openai". Only "openai" (case-
// insensitive) activates the real OpenAI-compatible client; any other value
// (including unset/empty) selects the stub, so a typo fails SAFE to today's
// behavior rather than to a broken backend. The base URL/model/key come from
// AGENT_INFERENCE_BASE_URL (default the host Ollama), AGENT_INFERENCE_MODEL
// (default phi4-mini), and AGENT_INFERENCE_API_KEY (optional — empty for the local
// no-key path, set for a gateway/cloud target). The HTTP round-trip timeout comes
// from AGENT_INFERENCE_TIMEOUT_SECONDS (#1079, default inference.DefaultTimeout) so a
// local 8B cold-start can be given a longer ceiling than the 60s that was cutting it
// off. Returns the chosen backend name for the startup log too.
func selectInferenceBackend() (inferFunc, string) {
	backend := strings.ToLower(strings.TrimSpace(getEnv("AGENT_INFERENCE_BACKEND", "stub")))
	if backend != "openai" {
		return nil, "stub"
	}
	client := inference.New(
		getEnv("AGENT_INFERENCE_BASE_URL", inference.DefaultBaseURL),
		getEnv("AGENT_INFERENCE_API_KEY", ""),
		getEnv("AGENT_INFERENCE_MODEL", inference.DefaultModel),
		inference.WithTimeout(secondsEnv("AGENT_INFERENCE_TIMEOUT_SECONDS", inference.DefaultTimeout)),
	)
	return client.Infer, "openai"
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
	// LIVE LLM budget override (#964): AGENT_LLM_MAX_REQUESTS_PER_MINUTE raises the
	// per-minute LLM request cap above the llm.max_requests_per_minute flag default
	// (6/min, which exhausts under the eval-cycle rate). 0 = no override (the flag
	// governs), so the default-off path is unchanged. Read once at startup and applied
	// to every per-game loop below, mirroring the shared budget instance itself.
	llmBudgetCapOverride := llmBudgetOverride()
	if llmBudgetCapOverride > 0 {
		slog.Info("LLM request budget override enabled (#964)", "max_per_minute", llmBudgetCapOverride)
	}
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
	// Inference backend selection (#1048, per the #1046 design). ONE OpenAI-compatible
	// client (or nil for the stub default) feeds BOTH the decision chain (here) AND the
	// shadow-experiment proposer (below) — Go structural typing means a single Infer
	// implementation satisfies decision.Backend and experiment.Backend. Default = stub
	// → nil delegate → no behavior change (the chain keeps its not-implemented sentinel
	// and degrades to rules; the proposer keeps proposeBackend{} and proposes nothing).
	// AGENT_INFERENCE_BACKEND=openai points it at the host Ollama (or a gateway) for a
	// real local inference path with no key.
	inferDelegate, inferBackendName := selectInferenceBackend()
	inferBaseURL := getEnv("AGENT_INFERENCE_BASE_URL", inference.DefaultBaseURL)
	inferModel := getEnv("AGENT_INFERENCE_MODEL", inference.DefaultModel)
	slog.Info("Inference backend selected (#1048)",
		"backend", inferBackendName,
		"base_url", inferBaseURL,
		"model", inferModel,
		"api_key_set", getEnv("AGENT_INFERENCE_API_KEY", "") != "",
	)
	// Inference cold-start pre-warm (#1130). When a REAL backend is configured
	// (inferDelegate != nil, AGENT_INFERENCE_BACKEND=openai), fire ONE best-effort
	// throwaway Infer in the background so the host Ollama model is resident before
	// the first real decision/proposal pays the ~30-100s cold load. No-op for the
	// stub default (nil delegate) → byte-identical startup. Bounded by the configured
	// inference timeout, non-blocking, non-fatal — see prewarmInference.
	prewarmInference(ctx, inferDelegate, inferModel,
		secondsEnv("AGENT_INFERENCE_TIMEOUT_SECONDS", inference.DefaultTimeout))
	legacyEndpoints := decision.Endpoints{
		Cloud:     getEnv("AGENT_CLOUD_ENDPOINT", ""),                    // #742 credential-blocked: empty = permanently unreachable
		Jetson:    getEnv("AGENT_JETSON_ENDPOINT", "jetson:11434"),       // #738 Ollama on the Jetson; unresolvable in dev
		Localhost: getEnv("AGENT_LOCALHOST_ENDPOINT", "localhost:11434"), // #739 Ollama locally; unreachable unless running
	}
	// #964: when a REAL inference backend is configured (AGENT_INFERENCE_BACKEND=openai),
	// build a Resolver whose chain TOP is the configured backend itself — named after
	// AGENT_INFERENCE_MODEL, probed at the host:port parsed from AGENT_INFERENCE_BASE_URL,
	// Infer-delegating to the SAME client the proposer uses. resolve() always starts at
	// that tier, so a reachable backend yields a non-nil Backend and the decision loop
	// reaches mode=llm instead of degrading to rules because the default phi4-mini model
	// flag pointed at an unreachable localhost tier. A single AGENT_INFERENCE_* config now
	// drives the backend request, the resolver's availability probe, AND the inference.used
	// attribution. When inferDelegate is nil (the stub DEFAULT) the legacy DefaultChainWithInfer
	// resolver is built unchanged — byte-identical degrade-to-rules behavior.
	var sharedResolver *decision.Resolver
	if inferDelegate != nil {
		probeAddr := decision.InferenceProbeAddr(inferBaseURL)
		slog.Info("Inference-backend resolver tier wired (#964)",
			"model", inferModel, "probe_addr", probeAddr)
		sharedResolver = decision.NewInferenceResolver(decision.InferenceTier{
			Model: inferModel,
			Addr:  probeAddr,
			Infer: inferDelegate,
		}, legacyEndpoints, decision.DefaultProbeInterval)
	} else {
		sharedResolver = decision.NewResolver(
			decision.DefaultChainWithInfer(legacyEndpoints, inferDelegate),
			decision.DefaultProbeInterval)
	}
	// The resolver's background probe ticker is started by run() (#923), which owns
	// the agent's goroutine lifecycle, so it is stopped on ctx cancellation alongside
	// every other runtime goroutine.
	// Shared rolling cross-game context window (#929, M7-2). Constructed ONCE here as
	// GLOBAL cross-session memory — one bounded ring of recent game summaries shared
	// across every per-game loop, exactly like the budget/resolver above. The
	// gamesummary coordinator (below) records each freshly-built Summary into it on
	// game end, and every per-game Loop reads Recent(N) from it on the llm path to
	// render the prompt's narrative context block (N = llm.context_games, live).
	sharedContextWindow := gamewindow.NewStore()
	// Intervention-effect follow-up window (#918): how long after a dispatched
	// intervention the loop re-samples the game's fitness signals to measure the
	// effect. Read ONCE at startup from AGENT_EFFECT_WINDOW_SECONDS (a float of
	// seconds); a non-positive/unparseable/unset value keeps decision.DefaultEffectWindow.
	effectWindow := effectWindowFromEnv()
	if effectWindow != decision.DefaultEffectWindow {
		slog.Info("Intervention-effect follow-up window overridden (#918)", "window", effectWindow)
	}
	probeDecisions := strings.EqualFold(getEnv("AGENT_PROBE_DECISIONS", ""), "true")
	if probeDecisions {
		slog.Warn("Probe decisions enabled (demo/verification mode)",
			"interval", probeInterval)
	}
	// Forward-declared so the per-game loop factory can close over it to wire the
	// async-inference re-validation source (#917): each loop re-fetches its game's
	// CURRENT context from this Multiplexer when an async inference result lands. It is
	// assigned below (mux = ...) before any signal arrives, so the closure never sees nil.
	var mux *gamecontext.Multiplexer
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
		// Apply the LIVE budget override (#964): when AGENT_LLM_MAX_REQUESTS_PER_MINUTE is
		// set it replaces the llm.max_requests_per_minute flag value as the per-minute cap,
		// so a real-inference dry-run is not gated by llm_budget_exhausted under the eval
		// rate. 0 (unset) is ignored — the flag governs, default-off path unchanged.
		loop.SetLLMBudgetOverride(llmBudgetCapOverride)
		// Inject the SHARED inference resolver (#741): every per-game loop resolves the
		// fallback chain against the same availability cache, so a gate-admitted llm
		// cycle reports the honest inference.used tier and any degradation reason.
		loop.SetResolver(sharedResolver)
		// Async inference wiring (#917): make the LLM call non-blocking. On a
		// gate-admitted, reachable-tier cycle the loop FIRES Infer in its own goroutine
		// and returns promptly; when the result lands it re-validates against this
		// game's CURRENT context (fetched from the Multiplexer by game_id) and applies
		// or discards it. SetGameID tells the apply path which partition to re-read;
		// SetContextProvider is the re-validation source; SetRootContext bounds every
		// fired goroutine to the agent's lifetime so shutdown cancels in-flight calls.
		loop.SetGameID(gameID)
		loop.SetContextProvider(decision.MuxContextProvider{Mux: mux})
		loop.SetRootContext(ctx)
		// Intervention-effect MEASUREMENT window (#918): after a dispatched intervention,
		// the loop samples THIS game's fitness/objective signals again after this window
		// and emits the before/after delta (agent.intervention.effect span +
		// agent_intervention_effect_delta metric), reusing the SetContextProvider source
		// above to re-read the game's CURRENT signals. The follow-up goroutine is bounded
		// by SetRootContext so shutdown cancels it cleanly (no leak). Tunable at startup
		// via AGENT_EFFECT_WINDOW_SECONDS; a non-positive/unset value keeps the default.
		loop.SetEffectWindow(effectWindow)
		// Intervention timeline wiring (#945): feed each decision's dispatched/blocked
		// outcome into the #916 rolling narrative of the SAME game this loop serves.
		// The closure captures THIS partition's game_id and routes through the
		// Multiplexer (mux.AppendInterventionEvent), so the event lands in the timeline
		// of the game it targeted and nowhere else — the per-game isolation (#845) the
		// multiplexer already enforces for signals, extended to interventions. This
		// mirrors SetContextProvider's #917 wiring (a per-game closure over the same
		// mux+gameID). runDecision calls it for BOTH dispatched and blocked decisions,
		// covering the sync path AND the async apply path (which also runs runDecision).
		// The timeline (and thus the #916/#928/#929 narratives + the #960 proposer
		// prompt) now includes the agent's own intervention history.
		loop.SetInterventionRecorder(func(intervention, serial string, blocked bool) {
			mux.AppendInterventionEvent(gameID, intervention, serial, blocked)
		})
		// Inject the SHARED rolling cross-game context window (#929, M7-2): every
		// per-game loop reads the SAME recent-games window, so the llm path renders the
		// last N game summaries into the prompt's narrative context block — the agent's
		// global cross-session memory, mirroring the shared budget/resolver. The async
		// path (above) reads it in runInfer; the sync path (un-wired loops) in llmDecide.
		loop.SetContextWindow(sharedContextWindow)
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
	// #1080: route the retro inference attribution through the SAME shared resolver the
	// per-game loops use (SetResolver above), so when AGENT_INFERENCE_BACKEND=openai the
	// retro reports the configured backend (gemma4 @ AGENT_INFERENCE_BASE_URL) instead of
	// resolving the agent.model flag's unreachable phi4-mini legacy tier. With the stub
	// default the resolver has no configured tier and the chain is unreachable, so the
	// retro degrades to used="none"/no_backend_available exactly as before.
	retro.SetResolver(sharedResolver)

	// Game narrative builder (#928, M7-1): on the SAME GameActive true->false
	// transition, aggregate the partition's pre-reset snapshot (live signals + the
	// #916 rolling timeline) into a compact JSON game summary written to disk
	// (AGENT_GAME_SUMMARY_DIR, default gamesummary.DefaultDir), one file per game,
	// for BOTH real and shadow games. It is a SEPARATE once-per-game component from
	// the retro — the summary must run unconditionally (no LLM budget/eligibility),
	// so it owns its own dedupe and is chained alongside retro.OnGameEnd below
	// (chainGameEnd) on the single store hook. The aggregation is itself an
	// agent.game.summary span.
	summaryDir := gamesummary.DirFromEnv()
	summaries := gamesummary.NewCoordinator(gamesummary.NewWriter(summaryDir), logger)
	// Advance the M7-2 rolling window on every game end (#929): the coordinator
	// Records each freshly-built Summary into the shared window BEFORE the disk write,
	// so the cross-game context the llm path injects reflects a game the instant it
	// ends, with no disk re-read. One observer, the same global window every loop reads.
	summaries.SetObserver(sharedContextWindow)
	slog.Info("Game narrative builder enabled (#928, M7-1)", "summary_dir", summaryDir)
	slog.Info("Rolling game context window enabled (#929, M7-2)", "retention_cap", gamewindow.RetentionCap)

	// M7-4 shadow-experiment proposer (#931). The agent turns its rolling game
	// narrative (#929 window) + fitness signals into a structured {flag, value}
	// experiment and submits it through the experiment Gate — shadow-scoped, safe
	// by construction (#932). The Gate writes the game flagset (GAME_FLAG_PATH /
	// game.json); the Proposer NEVER writes it directly.
	//
	// The Backend is the inference seam (Backend.Infer): today proposeBackend is the
	// sentinel stub (no real Ollama/cloud transport until #738/#742), so every
	// propose cycle degrades to NO proposal, NO error — the agent does not propose.
	// The engine flag (agent.code_improvement.engine) selects the backend; it is
	// read live and recorded on the span even though it only selects the stub now.
	// The vocab provider reads the LIVE game.json flag keys so the model is
	// constrained to (and the decoder validates against) the actual flag surface.
	experimentGate := experiment.NewGate(experiment.NewWriterFromEnv(logger), nil, logger)
	// Feed the SAME inference backend (#1048) to the proposer that feeds the decision
	// chain above: when AGENT_INFERENCE_BACKEND=openai, inferDelegate is the real
	// OpenAI-compatible client and proposeBackendFor wraps it as an experiment.Backend;
	// when nil (the stub default) it stays the inert proposeBackend{} so the propose
	// path degrades to no-proposal exactly as before.
	proposer := experiment.NewProposer(
		proposeBackendFor(inferDelegate),
		experimentGate,
		gameFlagVocab(getEnv("GAME_FLAG_PATH", experiment.DefaultGamePath)),
		nil,
		logger,
	)
	// Propose on each GAME END, behind the cadence floor (code_improvement.
	// min_interval_seconds, read live): proposing is an expensive LLM call, so it
	// fires far less often than a decision cycle. The hook renders the current
	// cross-game window as the narrative and fires ProposeOnce in a ctx-bounded
	// goroutine so a slow inference never blocks the game-end path. ProposeOnce
	// never returns an error (a failed/gated cycle is a recorded no-op).
	proposeOnGameEnd := func(c gamecontext.GameContext) {
		go func() {
			cfg := agentFlags.CodeImprovement(ctx)
			n := experiment.Narrative{
				ContextBlock: gamewindow.Render(sharedContextWindow.Recent(gamewindow.RetentionCap)),
			}
			if c.Session.GameMode != nil {
				n.GameMode = *c.Session.GameMode
			}
			proposer.ProposeOnce(ctx, n, experiment.ProposerConfig{
				Engine:      cfg.Engine,
				MinInterval: cfg.MinInterval,
			})
		}()
	}
	slog.Info("Shadow-experiment proposer enabled (#931, M7-4)", "game_flag_path", getEnv("GAME_FLAG_PATH", experiment.DefaultGamePath))

	// M7-8 code-improvement promotion surface (#936): build the Promoter with the
	// env-gated real clients (or the inert recording no-ops when not gated — the
	// SAFETY RAIL). The promotion mode/target are read LIVE per promotion, so an
	// operator can flip code_improvement.mode (issue→pr→autonomous) at runtime with
	// no restart. The live propose→validate→PROMOTE trigger that calls
	// promoter.Promote on a validated experiment is the documented follow-up (the
	// proposer is #931; the autonomous watch+revert reuses M7-7's seams); here the
	// gated promotion surface is constructed and held so the wiring + the env gate
	// are in place and observable.
	// FitnessStore (#965/#1017): the finalized-per-game fitness store. It is built HERE
	// (hoisted out of buildExperimentLoop) so it can back BOTH the experiment loop's
	// validation/anchor seams AND the autonomous Watcher's real-game fitness source
	// (#1057). The experiment loop's onGameEnd Records into it; the Watcher reads
	// recent REAL samples out of it.
	fitnessStore := experiment.NewFitnessStore(fitnessStoreCapacity())
	// #1100: give the retro span access to a finished experiment game's RECORDED
	// fitness, keyed by game_id, so an agent.llm.retro span carries experiment.fitness
	// alongside experiment.id / experiment.arm — making a retro joinable to the fitness
	// its arm scored. The experiment loop's onGameEnd (chained BEFORE retro.OnGameEnd
	// below) Records the settled sample into THIS store first, so the lookup sees it at
	// retro time. A non-experiment game has no sample (and carries no ExperimentID), so
	// the lookup is never even consulted for it — default-safe.
	retro.SetFitnessLookup(func(gameID string) (float64, bool) {
		s, ok := fitnessStore.Get(gameID)
		if !ok {
			return 0, false
		}
		return s.Fitness, true
	})
	// objResolver maps a promoted flag → its experiment's objective for the Watcher's
	// store reads. It is late-bound: the registry that knows the mapping is built inside
	// buildExperimentLoop (below), so the resolver is SET there once the registry exists.
	// Until then it resolves to "" (any-objective) — safe, just unfiltered.
	objResolver := &promote.LateObjectiveResolver{}

	promoter := buildPromoter(ctx, fitnessStore, objResolver, logger)
	slog.Info("Code-improvement promotion surface enabled (#936, M7-8)",
		"code_improvement_enabled", getEnv("AGENT_CODE_IMPROVEMENT_ENABLED", "false"),
		"target", getEnv("AGENT_CODE_IMPROVEMENT_TARGET", string(promote.TargetLocal)),
		"autonomous_enabled", getEnv("AGENT_AUTONOMOUS_ENABLED", "false"),
	)

	// M7 / #991 experiment cohort loop — FINAL ASSEMBLY (epic #982). buildExperimentLoop
	// constructs the experiment.Registry with the REAL seams (#976 spawner, #977
	// targeting Gate/Writer, #979 verdict, #980/#961 promoter). #1044 startup-gate
	// inversion: the loop is ALWAYS built + run now, and SELF-GATES each tick on the
	// LIVE agent.json experiments_enabled flag (default off, env AGENT_EXPERIMENTS_ENABLED
	// is the bootstrap default). When disabled — the default — the loop is BEHAVIORALLY
	// INERT: no rehydrate, no seed, no shadow spawn, no targeting write, no promotion —
	// the only residual work is one ~30s no-op tick goroutine. An off→on flip at runtime
	// starts it with no restart. The real-default promotion path stays
	// behind ALL of #961's gates (the promoter built above + the kill-switch via the
	// ConfigResolver).
	expLoop := buildExperimentLoop(agentFlags, promoter, fitnessStore, getEnv("GAME_FLAG_PATH", experiment.DefaultGamePath), logger)
	if expLoop != nil {
		// Bind the spawner's background game-drive goroutines to the agent's root
		// context so shutdown cancels in-flight shadow games (no goroutine leak).
		expLoop.spawner.SetRootContext(ctx)
		// Now the registry exists: install the flag→objective resolver the autonomous
		// Watcher uses to read the right per-objective recent-real fitness (#1057). Until
		// this point it resolved to "" (any-objective); from here it is precise.
		if expLoop.registry != nil {
			reg := expLoop.registry
			objResolver.Set(func(flagKey string) string {
				return objectiveForFlag(reg, flagKey)
			})
		}
	}

	// GameContext multiplexer (#845 PR B): one Store partition per game_id, plus the
	// fallback partition "" for unlabeled signals (zero-regression — single-game
	// mode collapses to exactly today's single-store behavior). The factory applies
	// the lifecycle TTLs/clock, skips the agent's own telemetry (otlp/agent
	// self-ingestion loop), and wires OnGameEnd per partition so the retrospective
	// fires per game on the right partition's state.
	// #917 declares `var mux` earlier so the per-game loop factory's ContextProvider
	// can close over it (the async apply path re-fetches the current context via the
	// multiplexer); assign it here.
	mux = gamecontext.NewMultiplexer(func(gameID string) *gamecontext.Store {
		// Prime with the holder's current values as the static fallback, then wire the
		// LIVE TTL sources (#927): EvictStale reads lifecycle.player_ttl_seconds /
		// lifecycle.session_grace_seconds from the shared holder at eviction time, so a
		// config-change is honored on the next tick with no restart. Every partition
		// created later shares the same holder source funcs.
		s := gamecontext.NewStore(lifecycle.PlayerTTL, lifecycle.SessionGrace, nil)
		s.SetTTLSources(lifecycleHolder.PlayerTTL, lifecycleHolder.SessionGrace)
		s.SetOwnService(ownService)
		// Chain the game-end consumers onto the single store hook (#928): the #991
		// experiment conclusion hook, the #844 retrospective, the M7-1 summary builder,
		// and the M7-4 proposer all fire on game end with the pre-reset snapshot. Each
		// has its OWN once-per-game dedupe, so chaining cannot make any double-fire, and
		// they are otherwise independent (no consumer reads another's in-memory state).
		//
		// ORDER (#1100): the experiment conclusion hook runs FIRST so it records the
		// game's finalized fitness into the #965 FitnessStore BEFORE the retro reads it
		// back by game_id (SetFitnessLookup) — making experiment.fitness available on the
		// agent.llm.retro span at retro time for an experiment arm. The chain is one
		// synchronous call on this goroutine, so "record-then-read" is guaranteed. The
		// experiment hook is nil when the loop is disabled (chainGameEnd skips it — zero
		// overhead in the default path); for an experiment-bound shadow game it folds the
		// fitness into the cohort journal via Registry.ConcludeGame, and for every
		// NON-experiment game it is a no-op (ConcludeGame ignores an unknown game_id), so
		// the existing retro/summary behavior is untouched.
		var experimentOnGameEnd func(gamecontext.GameContext)
		if expLoop != nil {
			experimentOnGameEnd = expLoop.onGameEnd
		}
		s.OnGameEnd = chainGameEnd(experimentOnGameEnd, retro.OnGameEnd, summaries.OnGameEnd, proposeOnGameEnd)
		return s
	})
	mux.SetOwnService(ownService)

	pipe := newPipeline(mux, loops, lifecycle.PlayerTTL).
		// Hot-reload the evaluate gate's freshness window in lockstep with the store
		// eviction TTL (#927): both read lifecycle.player_ttl_seconds from the holder.
		withPlayerTTLSource(lifecycleHolder.PlayerTTL).
		withInfra(infraStore, infraLoop)

	// Shadow-game runner (#778): when AGENT_SHADOW_GAME=true, sweep any orphaned
	// reserved controllers from a prior crashed run, then run ONE mock-only game
	// to completion in the background. The OTLP receiver keeps serving; the run does
	// not block startup. The default (unset) leaves the agent inert.
	var shadow func(context.Context)
	if gamerunner.Enabled() {
		shadow = func(ctx context.Context) { runShadowGame(ctx, logger) }
	}

	// The #991 experiment cohort loop's goroutine, owned by run() so it is torn down
	// (AbortAll) and joined on shutdown. Nil when the opt-in is off (the default).
	var experiments func(context.Context)
	if expLoop != nil {
		experiments = expLoop.run
	}

	// Hand the goroutine-owning runtime core to run (#923): the OTLP gRPC receiver,
	// health server, eviction ticker, the shared resolver's probe ticker, and the
	// graceful-shutdown path all live there so the start -> cancel -> drain sequence
	// is testable for goroutine leaks. The eviction cadence is HOT-RELOADED from the
	// holder (#927) via evictIntervalSrc: after each tick run re-reads
	// lifecycle.evict_interval_seconds and Resets the ticker only when it moved (a
	// lock-free atomic load, so the steady-state path costs nothing).
	components := &serverComponents{
		listenAddr:       listenAddr,
		healthAddr:       healthAddr,
		logger:           logger,
		pipe:             pipe,
		mux:              mux,
		loops:            loops,
		resolver:         sharedResolver,
		evictInterval:    lifecycleHolder.EvictInterval(),
		evictIntervalSrc: lifecycleHolder.EvictInterval,
		shadow:           shadow,
		experiments:      experiments,
	}
	if err := components.run(ctx); err != nil {
		slog.Error("gRPC server failed", "error", err)
		os.Exit(1)
	}
}

// errProposeBackendNotImplemented is the sentinel proposeBackend.Infer returns: no
// real propose-stage inference transport exists yet (the Ollama/cloud backend is
// hardware-/credential-blocked, #738/#742), exactly like decision.endpointBackend.
// The Proposer treats this as "degrade to no proposal this cycle" (recorded, no
// error), so the M7-4 propose path is fully wired and inert until a real backend
// lands. Mirrors the #739 intervention path, which degrades to rules identically.
var errProposeBackendNotImplemented = errors.New("code_improvement: propose backend not implemented (real Ollama/cloud transport blocked on #738/#742)")

// proposeBackend is the production propose-stage inference backend selected by
// agent.code_improvement.engine. It satisfies experiment.Backend structurally
// (Infer(ctx, llm.Prompt) (string, error)). Today every engine value resolves to
// this inert stub — Infer always returns the sentinel, so the agent proposes
// nothing. When a real generative backend is wired (#738/#742) this is where the
// engine flag would select it; until then the wiring degrades to nothing SAFELY.
type proposeBackend struct{}

func (proposeBackend) Infer(context.Context, llm.Prompt) (string, error) {
	return "", errProposeBackendNotImplemented
}

// inferBackend adapts a bare inferFunc into an experiment.Backend (#1048). The
// proposer takes a Backend (an interface with one Infer method); this lets the same
// inference delegate that feeds the decision chain feed the proposer too, with no
// second client instance — one OpenAIBackend, both seams.
type inferBackend struct {
	infer inferFunc
}

func (b inferBackend) Infer(ctx context.Context, prompt llm.Prompt) (string, error) {
	return b.infer(ctx, prompt)
}

// proposeBackendFor selects the proposer's inference backend (#1048): the real
// OpenAI-compatible client (wrapped as an experiment.Backend) when one was selected,
// or the inert proposeBackend{} stub when delegate is nil (the default) — so the
// propose path degrades to no-proposal exactly as before when inference is stubbed.
func proposeBackendFor(delegate inferFunc) experiment.Backend {
	if delegate == nil {
		return proposeBackend{}
	}
	return inferBackend{infer: delegate}
}

// gameFlagVocab returns the live set of game.json flag keys at path — the
// vocabulary the proposer constrains the model to and the decoder validates
// against. It re-reads the file on every call so a hot-reloaded game.json (the
// agent writes experiments into it) is reflected immediately. Any read/parse
// error returns an empty set, which fail-closes the decoder (nothing is
// proposable until the file is readable again) — the safe reading, since a model
// reply naming a flag we cannot confirm exists must never reach the Gate.
func gameFlagVocab(path string) func() map[string]struct{} {
	return func() map[string]struct{} {
		raw, err := os.ReadFile(path)
		if err != nil {
			return nil
		}
		var doc struct {
			Flags map[string]json.RawMessage `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			return nil
		}
		set := make(map[string]struct{}, len(doc.Flags))
		for k := range doc.Flags {
			set[k] = struct{}{}
		}
		return set
	}
}
