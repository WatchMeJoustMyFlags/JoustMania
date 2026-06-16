package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamerunner"
	"github.com/joustmania/agent/promote"
)

// experiment_loop.go is the FINAL ASSEMBLY of the experiment / cohort framework
// (#991, epic #982): it constructs the experiment.Registry with the REAL injected
// seams and runs the cohort loop inside the agent — declare an experiment, spawn
// shadow games into its arms, fold their conclusions into the journal, reach a
// verdict, and (gated) route a promote verdict to the #961 Promoter.
//
// OVERRIDING SAFETY (#991/#1044): the ENTIRE loop is gated behind the LIVE
// experiments_enabled flag (env AGENT_EXPERIMENTS_ENABLED is the bootstrap default,
// false) AND the agent kill-switch (agent.json `enabled`). Since #1044 the loop is
// ALWAYS built + run (so an off→on flag flip starts it with no restart), but while
// disabled — the default — it is BEHAVIORALLY INERT: no shadow game is spawned, no
// targeting is written, no promotion is attempted, the rehydrate+seed bootstrap is
// deferred (startedOnce), and AllocateAndSpawn is skipped. The only residual work is
// the registry/spawner/gate structs and one ~30s no-op tick (a no-op AbortAll + a few
// live flag reads) — observably inert, though not literally zero goroutines. The
// real-DEFAULT promotion path stays behind ALL of #961's existing gates
// (AGENT_CODE_IMPROVEMENT_ENABLED + token + kill-switch) — this file only ROUTES a
// concluded experiment to that already-gated Promoter; it adds no new privilege.

const (
	// envExperimentsEnabled is the distinct opt-in for the experiment LOOP itself
	// (separate from #961's AGENT_CODE_IMPROVEMENT_ENABLED, which gates the
	// real-default promotion action). Default false ⇒ the loop never runs.
	envExperimentsEnabled = "AGENT_EXPERIMENTS_ENABLED"

	// envExperimentTick overrides the AllocateAndSpawn cadence (seconds). The
	// registry refills free shadow capacity on each tick.
	envExperimentTick = "AGENT_EXPERIMENT_TICK_SECONDS"

	// envCompletedGrace overrides the bounded-grace backstop window (seconds) for a
	// COMPLETED shadow game whose telemetry game_active=0 datapoint never arrives
	// (#1020). After this grace elapses without ConcludeGame having folded the game,
	// onGameTerminal releases its in-flight slot non-counting so the
	// effective_concurrency=1 loop cannot deadlock on a dropped completed datapoint.
	envCompletedGrace = "AGENT_EXPERIMENT_COMPLETED_GRACE_SECONDS"

	// Seed-experiment env vars (the simplest env-gated declaration trigger; see
	// the declaration-trigger note below). When envSeedFlag is set the loop
	// Declares ONE experiment at startup so the cohort loop has something to run.
	envSeedFlag       = "AGENT_EXPERIMENT_SEED_FLAG"       // game.json flag key to experiment on
	envSeedValue      = "AGENT_EXPERIMENT_SEED_VALUE"      // experimental value (number/bool/JSON object|array/string)
	envSeedObjective  = "AGENT_EXPERIMENT_SEED_OBJECTIVE"  // fitness objective (default balanced)
	envSeedTargetN    = "AGENT_EXPERIMENT_SEED_TARGET_N"   // target games per arm
	envSeedHypothesis = "AGENT_EXPERIMENT_SEED_HYPOTHESIS" // free-text hypothesis

	// envDynamicMaxConcurrent caps the number of LIVE experiments the heuristic
	// dynamic declarer (#995) will keep going at once — the over-declaration
	// backstop. It is clamped to the shadow-game capacity (each experiment needs at
	// least one shadow slot to make progress). Unset/non-positive ⇒
	// defaultDynamicMaxConcurrent.
	envDynamicMaxConcurrent = "AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT"
)

// defaultDynamicMaxConcurrent is the default ceiling on simultaneously-live
// dynamically-declared experiments (#995). A small value keeps the autonomous
// declarer from flooding the registry; it is further clamped to the shadow-game
// capacity. The static env seed (if any) counts toward this ceiling too — the
// declarer reads the registry's LIVE count, which includes the seed.
const defaultDynamicMaxConcurrent = 3

// defaultExperimentTick is the AllocateAndSpawn cadence when unset. Shadow games
// are minutes-long, so a 30s refill tick is ample headroom over game duration.
const defaultExperimentTick = 30 * time.Second

// defaultCompletedGrace is the bounded-grace window (#1020) for a COMPLETED game
// whose telemetry game_active=0 datapoint may never arrive. It must COMFORTABLY
// exceed the worst-case telemetry-arrival lag so a game that concludes slightly
// late is still CONCLUDED with its real fitness sample (the grace fires only after
// ConcludeGame has had ample time and still has not folded the game). The
// game-end datapoint is emitted at session teardown and folded by onGameEnd within
// a handful of seconds in practice; 60s is an order of magnitude over that typical
// conclude latency yet still bounded — far short of leaking the slot "forever",
// which is the failure this backstop removes. Tunable via envCompletedGrace.
const defaultCompletedGrace = 60 * time.Second

// experimentsEnabled reports whether the experiment LOOP opt-in is on. This is the
// distinct, default-off gate (#991) — separate from the code-improvement gate.
func experimentsEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(envExperimentsEnabled)), "true")
}

// experimentLoop owns the agent's experiment cohort machinery: the Registry, the
// shadow spawner, the per-game-end conclusion hook, and the allocate/abort loop
// goroutines. It mirrors the serverComponents (#923) ownership model: run() starts
// its loop and cancels it on ctx; nothing here outlives the agent.
type experimentLoop struct {
	registry *experiment.Registry
	spawner  *shadowSpawner
	flags    *flags.Flags
	log      *slog.Logger

	// tracer emits the #1140 Slice C experiment-lifecycle spans (experiment.fitness /
	// experiment.evaluate / experiment.verdict) so the otherwise log-only "learn from
	// games" loop is an actual trace. Defaulted to the agent's tracer in
	// buildExperimentLoop; tests inject a recording tracer. A nil tracer is treated as
	// a no-op (the loop never panics on a span emission), so test wiring that does not
	// set it leaves behavior unchanged.
	tracer trace.Tracer

	// fitnessStore is the finalized-per-game fitness store (#965/#1017): onGameEnd
	// records every concluded game's settled fitness here, keyed by game_id, for
	// BOTH a shadow validation game (read back by the M7-7 StoreMeasurer) and a real
	// primary game (averaged by the StoreBaseline). It is the instrumentation the
	// Validator's Baseline/FitnessMeasurer seams read. nil ⇒ recording is skipped
	// (test wiring that does not exercise the validation path).
	fitnessStore *experiment.FitnessStore

	// validator is the M7-7 synthetic-validation gate (#935) wired over the LIVE #965
	// seams: it runs a candidate Proposal through real shadow games (the
	// shadowValidationRunner), measures their finalized fitness against the recent-real
	// baseline (the StoreMeasurer + StoreBaseline), and returns a promote/discard/revert
	// Decision. It is invoked ONLY from the gated, fail-closed (#1016)
	// autonomous-promotion path; merely holding it is inert. nil in test wiring that
	// does not exercise validation.
	validator *experiment.Validator

	// expDefaults are the BOOTSTRAP defaults for the migrated runtime knobs (#1044):
	// the values resolved ONCE at startup from the AGENT_EXPERIMENT_* env vars. Each
	// tick's live config(ctx) read falls back to these when the matching agent.json
	// flag is absent/unreadable — so flag overrides, env is the floor, and a fresh
	// agent.json reproduces byte-identical env behavior.
	expDefaults flags.ExperimentDefaults

	// seed, when set, is the one experiment Declared+Started when the loop FIRST
	// becomes enabled (the env-gated declaration trigger). Nil ⇒ no seed (the
	// registry rehydrates prior experiments and runs them, but declares nothing new).
	seed *experiment.Intent

	// dynamic is the heuristic dynamic declarer (#995). #1044: it is ALWAYS built
	// now; whether it actually declares each tick is gated by the LIVE
	// experiment_dynamic_enabled flag (checked in declareDynamicIfEnabled), so an
	// off→on flip starts dynamic declaration with no restart. nil only in tests that
	// do not wire it.
	dynamic *dynamicDeclarer

	// configOverride, when non-nil, supplies the live ExperimentConfig instead of
	// reading e.flags.Experiment — the injectable seam tests use to drive the
	// startup-gate inversion (off→on→off) and the per-knob live reads without a flagd
	// dependency. Production leaves it nil (the real flags client is consulted).
	configOverride func(ctx context.Context) flags.ExperimentConfig

	// startMu guards startedOnce so the deferred bootstrap runs EXACTLY once even
	// though allocateIfEnabled is called concurrently from several goroutines (tick,
	// immediate allocate, per-partition onGameEnd, onGameTerminal, grace timer).
	startMu sync.Mutex
	// startedOnce guards the one-time rehydrate+seed that runs the FIRST time the
	// loop observes experiments_enabled=true (the startup-gate inversion, #1044): the
	// loop is always running, but its declaration bootstrap is deferred until the
	// flag turns on, so an agent that starts with experiments off and is flipped on
	// later still rehydrates prior experiments and declares its seed.
	startedOnce bool

	// enabledOverride, when non-nil, supplies the kill-switch state instead of
	// reading e.flags — the injectable seam tests use to drive a disabled agent
	// without a flagd dependency. Production leaves it nil (the real flags client is
	// consulted). It governs BOTH AllocateAndSpawn and the #995 dynamic declarer, so
	// a test can prove the declarer is subordinate to the kill-switch.
	enabledOverride func(ctx context.Context) bool

	// fitness maps a game-end GameContext to a single fitness scalar fed to
	// ConcludeGame. Injected so it is overridable / testable.
	fitness gameFitnessFunc

	// completedGrace is the bounded-grace window (#1020): how long onGameTerminal
	// waits after a COMPLETED game before releasing its in-flight slot non-counting
	// if ConcludeGame has not folded it. Resolved from envCompletedGrace.
	completedGrace time.Duration

	// afterFunc schedules f to run after d, returning a stop func (true if it
	// cancelled a not-yet-fired timer). Defaults to a time.AfterFunc adapter;
	// tests inject a fake clock so the grace can fire without a real sleep.
	afterFunc func(d time.Duration, f func()) (stop func() bool)

	// graceMu guards the set of pending completed-grace timers so run() can stop
	// them on shutdown (no goroutine/timer leak past teardown).
	graceMu     sync.Mutex
	graceTimers map[string]func() bool
	// graceShutdown is set under graceMu by stopAllCompletedGrace and checked at the
	// top of fireCompletedGrace. time.AfterFunc(...).Stop() does NOT wait for an
	// already-firing callback, so on shutdown a grace goroutine can enter
	// fireCompletedGrace after run() has called stopAllCompletedGrace() and returned.
	// This flag makes that already-firing callback bail (drop its entry and return
	// without ReleaseGame/allocate) so no shadow game is spawned during/after
	// shutdown — honoring run()'s "no goroutine outlives run()" contract (#1020).
	graceShutdown bool
}

// gameFitnessFunc scores a finished game (its pre-reset GameContext) into one
// fitness scalar in [0,1] (higher = better), the per-game sample the cohort
// aggregator folds. The default (defaultGameFitness) reuses the M7 fitness engine.
type gameFitnessFunc func(gc gamecontext.GameContext, objective string) float64

// buildExperimentLoop constructs the experiment loop with the REAL seams. Since the
// #1044 startup-gate inversion the loop is ALWAYS built (no nil return) so an off→on
// flip of the live experiments_enabled flag starts it with no restart. While the flag
// is off — the default — the loop is BEHAVIORALLY INERT (it self-gates each tick: no
// rehydrate, no seed, no spawn, no targeting write, no promotion), so the disabled
// agent's experiment surface is observably dormant.
//
// Seams wired (the #991 assembly):
//   - TargetingWriter → experiment.NewGateTargetingWriter(gate, writer): Apply via
//     the #977 Gate (invariant-checked write), Strip via the Writer (#977 teardown).
//   - CohortVerdict   → experiment.NewVerdictFromEnv() (#979 min-N + effect-size).
//   - Promoter        → promote.NewExperimentPromoter(promoter, resolver) (#980 over
//     the #961 Promoter), resolver reading LIVE flags.Promotion + the kill-switch.
//   - ShadowSpawner   → the real outbound game-coordinator client (shadowSpawner).
func buildExperimentLoop(
	flagsClient *flags.Flags,
	promoter *promote.Promoter,
	fitnessStore *experiment.FitnessStore,
	gameFlagPath string,
	logger *slog.Logger,
) *experimentLoop {
	// #1044 startup-gate inversion: the loop is ALWAYS built now (no nil return).
	// experiments_enabled is a LIVE agent.json flag, so the loop must exist + run so
	// an off→on flip at runtime starts it with no restart. It SELF-GATES each tick on
	// the live flag (allocateIfEnabled / run): while disabled it does nothing —
	// rehydrate+seed are deferred (startedOnce) and AllocateAndSpawn is skipped — so
	// the disabled agent is behaviorally inert (it runs one no-op tick goroutine but
	// spawns/writes/promotes nothing). The env AGENT_EXPERIMENTS_ENABLED becomes the
	// BOOTSTRAP DEFAULT the flag falls back to.
	expDefaults := experimentDefaultsFromEnv()

	logger.Info("Experiment cohort loop constructed (#991/#1044) — gated LIVE by agent.json experiments_enabled",
		"env_enabled_default", expDefaults.Enabled,
		"env_dynamic_default", expDefaults.DynamicEnabled,
		"tick_default", expDefaults.Tick,
		"completed_grace", completedGrace(),
		"max_shadow_games", experiment.MaxShadowGamesFromEnv(),
		"effective_concurrency_default", expDefaults.ShadowEffectiveConcurrency,
		"seed_flag", os.Getenv(envSeedFlag))

	// TargetingWriter (#977): a Gate over a Writer pinned to the game flagset. The
	// Gate validates THE INVARIANT on every Apply; Strip un-nests a branch on
	// teardown. The Writer is shared by Gate (Apply) and the adapter (Strip).
	writer := experiment.NewWriter(gameFlagPath, logger)
	gate := experiment.NewGate(writer, nil, logger)
	targeting := experiment.NewGateTargetingWriter(gate, writer)

	// Promoter (#980/#961): adapt the already-built, env-gated #961 Promoter. The
	// ConfigResolver reads the LIVE promotion mode/target AND the agent kill-switch
	// each promotion, so the real-default path stays behind every existing gate.
	promo := promote.NewExperimentPromoter(promoter, promotionConfigResolver(flagsClient))

	// ShadowSpawner (#976): the real outbound game-coordinator client.
	spawner := newShadowSpawner(gamerunner.ConfigFromEnv(), logger)

	// FitnessStore (#965/#1017): the finalized-per-game fitness store onGameEnd
	// Records into and the M7-7 validation seams read from. It is the instrumentation
	// that turns the Validator's injected fitness reads into REAL ones. It is now built
	// by the CALLER (main.go) and passed in so the SAME store also backs the autonomous
	// Watcher's real-game fitness source (#1057). A nil store (a test or a caller that
	// did not build one) falls back to a fresh local store so the loop is still valid.
	if fitnessStore == nil {
		fitnessStore = experiment.NewFitnessStore(fitnessStoreCapacity())
	}

	// CohortVerdict (#979) + recent-real SECONDARY anchor (#992): the within-experiment
	// comparison gated by recent REAL play. The anchor reads the FitnessStore's
	// RecentReal mean for the experiment's objective via a nil-safe accessor; it fails
	// OPEN when no real-game baseline exists (the current mock/shadow-only reality), so
	// it never blocks the shadow loop. recentN/minSamples reuse the validation-baseline
	// knobs so the anchor and the M7-7 Validator agree on what "enough real data" means.
	verdict := experiment.NewVerdictFromEnv().WithRecentRealBaseline(
		recentRealBaselineAccessor(fitnessStore, validationBaselineRecentN(), validationBaselineMinSamples()),
		experiment.DefaultAnchorMargin,
	)

	registry := experiment.NewRegistry(experiment.RegistryConfig{
		Root:      journal.DirFromEnv(),
		Spawner:   spawner,
		Verdict:   verdict,
		Promoter:  promo,
		Targeting: targeting,
		Log:       logger,
	})

	loop := &experimentLoop{
		registry:       registry,
		spawner:        spawner,
		flags:          flagsClient,
		log:            logger,
		tracer:         otel.Tracer(experimentInstrumentationName),
		fitnessStore:   fitnessStore,
		expDefaults:    expDefaults,
		seed:           seedIntentFromEnv(),
		fitness:        defaultGameFitness,
		completedGrace: completedGrace(),
		dynamic:        newDynamicDeclarerFromEnv(gameFlagPath, nil),
	}
	// Build the M7-7 Validator (#935) over the LIVE seams (#965): the shadow
	// SyntheticRunner drives real shadow games via the spawner; the FitnessMeasurer +
	// Baseline read the finalized store; the Reverter strips shadow targeting via the
	// #977 Writer. It is stored on the loop for the (gated, fail-closed #1016)
	// autonomous-promotion path to invoke; merely building it is inert.
	loop.validator = experiment.NewValidator(
		experiment.NewStoreBaseline(fitnessStore, validatorObjectiveResolver(registry), validationBaselineRecentN(), validationBaselineMinSamples()),
		newShadowValidationRunner(spawner, fitnessStore, validationGameTimeout(), logger),
		experiment.NewStoreMeasurer(fitnessStore),
		experiment.NewWriterReverter(writer),
		nil, logger,
	)
	logger.Info("Dynamic experiment declarer constructed (#995/#1044) — gated LIVE by agent.json experiment_dynamic_enabled",
		"env_dynamic_default", expDefaults.DynamicEnabled,
		"candidates", os.Getenv(envDynamicCandidates))
	// Wire the #1014 terminal-outcome backstop: each spawned game signals its
	// terminal outcome here, and a non-concluding game releases its in-flight slot
	// directly (independent of the telemetry game_active=0 path), so the
	// effective_concurrency=1 loop can never deadlock on an errored game.
	spawner.SetTerminalCallback(loop.onGameTerminal)
	return loop
}

// promotionConfigResolver builds the #980 ConfigResolver from the live agent
// flags: the promotion mode/target come from flags.Promotion, and the kill-switch
// (Config.Enabled) from the agent `enabled` snapshot. Reading them live per
// promotion means flipping code_improvement.mode (or the kill-switch) takes effect
// on the next promotion with no restart — and a disabled agent can never reach the
// autonomous real-default path.
func promotionConfigResolver(f *flags.Flags) promote.ConfigResolver {
	return func(ctx context.Context) promote.Config {
		p := f.Promotion(ctx)
		return promote.Config{
			Mode:    promote.Mode(p.Mode),
			Target:  promote.Target(p.Target),
			Enabled: f.Evaluate(ctx).Enabled, // kill-switch: false ⇒ autonomous blocked
		}
	}
}

// run starts the experiment loop and blocks until ctx is cancelled, then returns.
// It is started in its own goroutine by serverComponents.run (#923) so its
// lifecycle is owned alongside every other agent goroutine and torn down on
// shutdown.
//
// STARTUP-GATE INVERSION (#1044): the loop is ALWAYS running now — experiments_enabled
// is a LIVE agent.json flag, not a build-time env gate. Each tick the loop reads the
// live ExperimentConfig and SELF-GATES:
//   - DISABLED (experiments_enabled false, the default) ⇒ AbortAll every live
//     experiment and do nothing else. No rehydrate, no seed, no allocate. This is
//     behaviorally inert (the only residual work is one no-op tick), and it subsumes
//     the kill-switch teardown (a disabled agent and a disabled-experiments flag both
//     tear the experiment surface down).
//   - ENABLED ⇒ on the FIRST enabled tick run the one-time rehydrate (#831 durable
//     resume) + seed declaration (startedOnce); then apply the live caps
//     (Reconfigure) and refill shadow capacity (allocate + dynamic declare). An
//     off→on flip therefore STARTS the loop (rehydrate+seed+allocate) and an on→off
//     flip STOPS it cleanly (AbortAll strips targeting + releases capacity; in-flight
//     shadow games conclude/release normally — no corruption).
//
// The tick CADENCE itself is live (experiment_tick_seconds): the ticker is re-armed
// whenever the resolved interval moves, mirroring the #927 eviction-ticker pattern,
// so retuning the cadence takes effect with no restart.
//
// SEED + DYNAMIC COMPOSITION (#995): unchanged. The seed (if set) is declared ONCE
// on the first enabled tick; the dynamic declarer then fills the rest each tick,
// bounded by the live concurrent-experiment backstop. The kill-switch (agent.json
// `enabled`) is ALSO honored each tick (agentEnabled), independent of
// experiments_enabled — either being off tears the surface down.
func (e *experimentLoop) run(ctx context.Context) {
	// First action immediately so an already-enabled agent accrues games without
	// waiting a full tick (and a disabled one tears down promptly), then refill on
	// the live cadence.
	interval := e.tickInterval(ctx)
	e.allocateIfEnabled(ctx)

	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			// Shutdown / kill-switch teardown: abort every live experiment so its
			// targeting branch is stripped and capacity released. Cancel any pending
			// completed-grace timers first so none fires after the loop returns (#1020).
			e.stopAllCompletedGrace()
			// Use a short background context so teardown's file I/O still runs after
			// ctx cancel.
			tctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			ids := e.registry.AbortAll(tctx, "agent shutdown")
			cancel()
			if len(ids) > 0 {
				e.log.Info("experiment: aborted live experiments on shutdown", "count", len(ids))
			}
			return
		case <-ticker.C:
			e.allocateIfEnabled(ctx)
			// Re-arm the ticker if experiment_tick_seconds was hot-reloaded (#1044),
			// mirroring the #927 eviction-ticker live-cadence pattern.
			if next := e.tickInterval(ctx); next > 0 && next != interval {
				interval = next
				ticker.Reset(interval)
				e.log.Info("experiment: tick cadence hot-reloaded (#1044)", "tick", interval)
			}
		}
	}
}

// config resolves the live ExperimentConfig for one tick (#1044): the configOverride
// seam (tests) takes precedence, else the live agent.json flags read via
// flags.Experiment with the env-derived bootstrap defaults as the fallback floor. A
// nil flags client (test wiring only) yields the bootstrap defaults verbatim.
func (e *experimentLoop) config(ctx context.Context) flags.ExperimentConfig {
	if e.configOverride != nil {
		return e.configOverride(ctx)
	}
	if e.flags == nil {
		return flags.ExperimentConfig{
			Enabled:                    e.expDefaults.Enabled,
			DynamicEnabled:             e.expDefaults.DynamicEnabled,
			Tick:                       e.expDefaults.Tick,
			DynamicMaxConcurrent:       e.expDefaults.DynamicMaxConcurrent,
			VerdictMinN:                e.expDefaults.VerdictMinN,
			VerdictMinPairs:            e.expDefaults.VerdictMinPairs,
			MaxGames:                   e.expDefaults.MaxGames,
			ShadowEffectiveConcurrency: e.expDefaults.ShadowEffectiveConcurrency,
		}
	}
	return e.flags.Experiment(ctx, e.expDefaults)
}

// tickInterval resolves the live AllocateAndSpawn cadence (experiment_tick_seconds),
// flooring a non-positive value at the env-derived default (durationFlag already
// guards this in the live path; the floor here covers the configOverride/test seam).
func (e *experimentLoop) tickInterval(ctx context.Context) time.Duration {
	d := e.config(ctx).Tick
	if d <= 0 {
		d = e.expDefaults.Tick
	}
	if d <= 0 {
		d = defaultExperimentTick
	}
	return d
}

// allocateIfEnabled refills shadow capacity ONLY while BOTH the live
// experiments_enabled flag (#1044) AND the agent kill-switch (agent.json `enabled`)
// are on. When either is off it AbortAll's every live experiment instead — the
// mid-run teardown that keeps the experiment loop subordinate to the same gates the
// decision loop obeys. When enabled, it lazily runs the one-time rehydrate+seed
// (startup-gate inversion), applies the live caps, then allocates.
func (e *experimentLoop) allocateIfEnabled(ctx context.Context) {
	cfg := e.config(ctx)
	if !cfg.Enabled {
		if ids := e.registry.AbortAll(ctx, "experiments_enabled flag off"); len(ids) > 0 {
			e.log.Warn("experiment: experiments_enabled flag off — aborted live experiments (#1044)", "count", len(ids))
		}
		return
	}
	if !e.agentEnabled(ctx) {
		if ids := e.registry.AbortAll(ctx, "kill-switch: agent disabled"); len(ids) > 0 {
			e.log.Warn("experiment: kill-switch active — aborted live experiments", "count", len(ids))
		}
		return
	}
	// First enabled tick: run the deferred one-time rehydrate + seed declaration (the
	// startup-gate inversion bootstrap, #1044).
	e.ensureStarted(ctx)
	// Apply the live runtime caps (#1044) before allocating so a hot-reloaded
	// concurrency / games-budget / min-N takes effect on THIS tick's allocation +
	// verdict evaluation.
	e.registry.Reconfigure(cfg.ShadowEffectiveConcurrency, cfg.MaxGames, cfg.VerdictMinN, cfg.VerdictMinPairs)
	// Heuristic dynamic declaration (#995): both gates are on (checked above) and the
	// declarer is opted-in (live experiment_dynamic_enabled, #1044), so let the agent
	// CHOOSE a fresh experiment to declare — bounded by the LIVE concurrent-experiment
	// backstop and the per-flag dedup. This runs BEFORE AllocateAndSpawn so a
	// freshly-Started experiment can accrue games on the same tick.
	e.declareDynamicIfEnabled(ctx, cfg)
	if n := e.registry.AllocateAndSpawn(ctx); n > 0 {
		e.log.Info("experiment: spawned shadow games this tick", "count", n,
			"in_flight", e.registry.TotalInFlight(), "capacity", e.registry.Capacity())
	}
}

// agentEnabled reports whether the agent kill-switch is ON (agent.json `enabled`
// true). It consults the enabledOverride seam first (test wiring), then the live
// flags client. A nil flags client (test wiring only) is treated as enabled —
// production always injects one (buildExperimentLoop) — so the leak test stays
// free of a flagd dependency while the real path always consults the kill-switch.
func (e *experimentLoop) agentEnabled(ctx context.Context) bool {
	if e.enabledOverride != nil {
		return e.enabledOverride(ctx)
	}
	if e.flags == nil {
		return true
	}
	return e.flags.Evaluate(ctx).Enabled
}

// declareDynamicIfEnabled lets the heuristic dynamic declarer (#995) CHOOSE and
// declare ONE fresh experiment per call when:
//   - the dynamic declarer is opted-in (dynamic != nil), AND
//   - the live-experiment count is below the concurrent-experiment backstop
//     (clamped to the shadow-game capacity — the over-declaration guard), AND
//   - the declarer can find a candidate game.json flag with no active experiment
//     and a distinct alternate value (the per-flag dedup).
//
// It is a NO-OP when the declarer is not wired (dynamic == nil) OR the LIVE
// experiment_dynamic_enabled flag is off (#1044), so the env-seed path is
// byte-identical for everyone who has not opted in — and an off→on flip of the flag
// starts dynamic declaration on the next tick with no restart. It is only ever
// reached from allocateIfEnabled, which has already confirmed BOTH experiments_enabled
// AND the kill-switch are on — so the declarer is subordinate to the SAME gates as
// every other declaration. At most one experiment is declared per call (one new flag
// explored per tick), keeping the declaration rate bounded; the round-robin cursor
// rotates coverage across ticks.
//
// The concurrent-experiment budget is the LIVE experiment_dynamic_max_concurrent
// (#1044, from cfg), clamped to the shadow capacity.
//
// CONCURRENCY (#1040 review): allocateIfEnabled is called concurrently from several
// goroutines (tick, immediate allocate, per-partition onGameEnd, onGameTerminal
// drive, fireCompletedGrace timer). The budget check, the per-flag dedup
// (ActiveFlags), the cursor advance, and Declare must be ATOMIC as a unit — three
// separate registry-lock acquisitions plus an unsynchronized cursor would otherwise
// let N concurrent callers all pass the LiveCount<budget check and all declare
// (TOCTOU over-declaration), and would data-race d.cursor. We hold the declarer's
// own mutex (e.dynamic.mu) around the WHOLE decision path. That mutex WRAPS the
// registry calls and the registry never calls back into the declarer, so there is
// no lock-ordering deadlock with registry.mu.
func (e *experimentLoop) declareDynamicIfEnabled(ctx context.Context, cfg flags.ExperimentConfig) {
	if e.dynamic == nil {
		return // declarer not wired (test path): env-seed behavior unchanged.
	}
	if !cfg.DynamicEnabled {
		return // LIVE experiment_dynamic_enabled off (#1044): env-seed behavior only.
	}
	// Serialize the entire select→budget-check→dedup→declare sequence so only one
	// dynamic declare is ever in flight (closes the TOCTOU over-declaration window)
	// and d.cursor is mutated under the lock (closes the data race).
	e.dynamic.mu.Lock()
	defer e.dynamic.mu.Unlock()

	// Over-declaration backstop: stop once the LIVE experiment set fills the
	// concurrent-experiment budget. The budget is the LIVE
	// experiment_dynamic_max_concurrent (#1044), clamped to the shadow capacity
	// because an experiment with no shadow slot to ever run is pointless. A
	// non-positive live value falls back to the env-derived default.
	budget := cfg.DynamicMaxConcurrent
	if budget <= 0 {
		budget = e.expDefaults.DynamicMaxConcurrent
	}
	if capacity := e.registry.Capacity(); capacity > 0 && budget > capacity {
		budget = capacity
	}
	if e.registry.LiveCount() >= budget {
		return
	}
	// Per-flag dedup: never stack a second experiment on a flag already under test.
	intent, ok := e.dynamic.Declare(e.registry.ActiveFlags())
	if !ok {
		return // nothing declarable this tick (no free candidate flag).
	}
	if err := e.declareAndStart(ctx, intent); err != nil {
		// A type-mismatch / unknown-flag rejection at Start fails closed (no shadow
		// game runs); log it and move on — the next tick rotates to another flag.
		e.log.Warn("experiment: dynamic declare/start failed (#995)",
			"flag_key", intent.FlagKey, "objective", intent.Objective, "error", err)
		return
	}
	e.log.Info("experiment: dynamic declarer declared a new experiment (#995)",
		"flag_key", intent.FlagKey, "value", intent.ExperimentalValue, "objective", intent.Objective)
}

// ensureStarted runs the deferred ONE-TIME bootstrap the FIRST time the loop
// observes experiments_enabled=true (the startup-gate inversion, #1044): rehydrate
// the durable journal (#831 resume) and declare+start the env seed. It is
// idempotent + concurrency-safe (startMu + startedOnce), so the many concurrent
// callers of allocateIfEnabled run it exactly once. A rehydrate error is logged and
// the bootstrap is STILL marked done (we do not want to re-rehydrate every tick); a
// seed declare error is logged and tolerated (the loop proceeds without the seed).
//
// ONE-SHOT ENV SEED (#1051): startedOnce latches for the lifetime of the loop, so on
// an off→on→off→on cycle the env seed is declared EXACTLY ONCE — on the first enable
// — and is NOT re-declared on any later re-enable. This is intentional: the env seed
// is a one-shot STARTUP trigger, not a per-enable action. After a teardown
// (on→off AbortAll) the seed experiment is terminal, so a re-enable resumes work via
// rehydrate (any non-terminal experiment in the durable journal) + the dynamic
// declarer (if experiment_dynamic_enabled is on) — never by re-running the env seed.
// (TestExperimentsEnabled_OffOnOff pins this re-enable behavior.)
func (e *experimentLoop) ensureStarted(ctx context.Context) {
	e.startMu.Lock()
	defer e.startMu.Unlock()
	if e.startedOnce {
		return
	}
	e.startedOnce = true
	if err := e.rehydrate(); err != nil {
		e.log.Error("experiment: rehydrate from journal failed (continuing without prior experiments)", "error", err)
	}
	if e.seed != nil {
		if err := e.declareAndStart(ctx, *e.seed); err != nil {
			e.log.Error("experiment: seed declare/start failed", "error", err)
		}
	}
}

// rehydrate rebuilds the live registry from the durable journal on startup.
func (e *experimentLoop) rehydrate() error {
	ids, err := journal.List(journal.DirFromEnv())
	if err != nil {
		return err
	}
	if len(ids) == 0 {
		return nil
	}
	if err := e.registry.Rehydrate(ids); err != nil {
		return err
	}
	e.log.Info("experiment: rehydrated experiments from journal", "candidates", len(ids), "live", len(e.registry.Live()))
	return nil
}

// declareAndStart declares one experiment and writes its shadow targeting,
// transitioning it to RUNNING so it can accrue shadow games.
func (e *experimentLoop) declareAndStart(ctx context.Context, in experiment.Intent) error {
	id, err := e.registry.Declare(in)
	if err != nil {
		return err
	}
	return e.registry.Start(ctx, id)
}

// onGameEnd is the conclusion hook chained onto the Store's OnGameEnd (#928): when
// a shadow game that belongs to an experiment ends, fold its fitness sample into
// the matching arm via ConcludeGame. A game not bound to any experiment (no
// ExperimentID, or an id the registry does not own) is ignored by ConcludeGame, so
// this hook is a safe no-op for every NON-experiment game — it never changes the
// existing per-game retro/summary path. After a conclusion it nudges
// AllocateAndSpawn so the freed slot is refilled promptly (capacity churn).
func (e *experimentLoop) onGameEnd(gc gamecontext.GameContext) {
	// The coordinator emits game_duration_seconds ONLY at game end (one gauge set in
	// the session teardown), so for an agent-spawned shadow game it routinely loses
	// the race against the game_active=0 datapoint that fires THIS hook — leaving
	// gc.Session.DurationSeconds nil at conclusion. That nil made every
	// duration-based objective (endurance/accelerate) skip its fitness function and
	// fold the worst-case 0 (#997). Recover the real elapsed time from the partition's
	// own start→end phase timeline so a concluded game carries a meaningful,
	// objective-relevant duration even when the end-only gauge has not arrived. This
	// recovery now applies to REAL games too (#1017 §3 item 2) so a dropped end gauge
	// cannot poison the recent-real baseline with a worst-case 0.
	gc = withEffectiveDuration(gc)
	duration := 0.0
	if gc.Session.DurationSeconds != nil {
		duration = *gc.Session.DurationSeconds
	}

	if gc.ExperimentID == "" {
		// Not an experiment game. Pre-#965 this returned immediately and the game's
		// fitness was dropped. The #1017 instrumentation gap: a REAL primary game's
		// fitness is COMPUTABLE today (same EvaluateFitness, substrate-agnostic) but is
		// never stored — leaving the Validator's Baseline seam with no real-game signal.
		// Record it into the finalized-per-game store (keyed by game_id) so the
		// StoreBaseline can read the mean of the last N real games. This is a pure ADD:
		// a non-experiment game still does nothing else (no cohort fold, no allocate).
		e.recordFinalizedRealFitness(gc, duration)
		return
	}

	objective := ""
	if cv, ok := e.registry.CompactView(gc.ExperimentID); ok {
		objective = cv.Intent.Objective
	}

	// #1140 Slice C: emit the experiment-lifecycle spans around the EXISTING fitness /
	// conclusion computations — observability only, no change to the values computed,
	// their order, or the loop's control flow. Each span is Linked to the originating
	// game trace via the SHARED decision.GameTraceLink primitive (#1133/#1157), passing
	// BOTH the game trace_id AND its root span_id so Jaeger highlights the game-start
	// span (#1157) instead of an all-zero span id. Sharing the primitive (rather than a
	// replica) keeps the experiment.* Links at parity with the decision/retro Links.
	gameLink := decision.GameTraceLink(gc.GameTraceID, gc.GameTraceSpanID)

	// experiment.fitness: the per-shadow-game fitness scalar the cohort aggregator folds.
	fitness := e.scoreGameFitness(gc, objective, gameLink)
	// Record the SHADOW game's finalized fitness keyed by game_id (#965) so the M7-7
	// StoreMeasurer can read a validation batch's fitness back. This is the same
	// scalar ConcludeGame folds into the cohort journal — captured for the validation
	// path too.
	e.recordFinalizedFitness(gc, objective, fitness, duration)

	// experiment.evaluate: fold the concluded game into its arm and observe the
	// resulting lifecycle status; experiment.verdict (a child) records the rolling
	// paired-difference verdict the registry computed, when one is present.
	status, err := e.concludeWithSpan(gc, fitness, duration, gameLink)
	if err != nil {
		e.log.Warn("experiment: conclude game failed", "game_id", gc.SessionID, "error", err)
		return
	}
	if status == experiment.StatusRunning {
		// A freed in-flight slot; refill it (respecting the kill-switch).
		e.allocateIfEnabled(context.Background())
	}
}

// scoreGameFitness computes one finished experiment game's fitness scalar (the
// existing gameFitnessFunc) inside the #1140 experiment.fitness span. The span is
// OBSERVABILITY ONLY — it wraps the SAME e.fitness(gc, objective) call and returns
// its result unchanged. It carries the experiment correlation (id / arm / game.id /
// objective) and the resulting experiment.fitness, and Links to the game trace when
// one is in scope. A nil tracer (test wiring) is a no-op: the fitness call runs
// directly with no span.
func (e *experimentLoop) scoreGameFitness(gc gamecontext.GameContext, objective string, gameLink []trace.SpanStartOption) float64 {
	if e.tracer == nil {
		return e.fitness(gc, objective)
	}
	attrs := []attribute.KeyValue{
		attribute.String(decision.AttrExperimentID, gc.ExperimentID),
		attribute.String(decision.AttrGameID, gc.SessionID),
	}
	if gc.Arm != "" {
		attrs = append(attrs, attribute.String(decision.AttrExperimentArm, gc.Arm))
	}
	if objective != "" {
		attrs = append(attrs, attribute.String(decision.AttrExperimentObjective, objective))
	}
	// #1174 consistency: stamp the game trace_id as a SEARCHABLE attribute too (retro
	// has both the Link and the attr; this span only had the Link). Empty => skip.
	if gc.GameTraceID != "" {
		attrs = append(attrs, attribute.String(decision.AttrGameTraceID, gc.GameTraceID))
	}
	opts := append([]trace.SpanStartOption{trace.WithAttributes(attrs...)}, gameLink...)
	_, span := e.tracer.Start(context.Background(), decision.SpanExperimentFitness, opts...)
	defer span.End()

	fitness := e.fitness(gc, objective)
	span.SetAttributes(attribute.Float64(decision.AttrExperimentFitness, fitness))
	return fitness
}

// concludeWithSpan folds the concluded game into its arm (the existing
// registry.ConcludeGame) inside the #1140 experiment.evaluate span and, when the
// conclusion produced a rolling verdict, emits a child experiment.verdict span. Both
// are OBSERVABILITY ONLY — the fold, the status, and the verdict are exactly what the
// registry computes; the spans record them, they do not change them. experiment.evaluate
// Links to the game trace. A nil tracer (test wiring) is a no-op: ConcludeGame runs
// directly with no span. The returned (status, err) are passed straight through.
func (e *experimentLoop) concludeWithSpan(gc gamecontext.GameContext, fitness, duration float64, gameLink []trace.SpanStartOption) (experiment.Status, error) {
	if e.tracer == nil {
		return e.registry.ConcludeGame(context.Background(), gc.SessionID, fitness, duration)
	}
	attrs := []attribute.KeyValue{
		attribute.String(decision.AttrExperimentID, gc.ExperimentID),
		attribute.String(decision.AttrGameID, gc.SessionID),
	}
	if gc.Arm != "" {
		attrs = append(attrs, attribute.String(decision.AttrExperimentArm, gc.Arm))
	}
	// #1174 consistency: stamp the game trace_id as a searchable attribute too. Empty => skip.
	if gc.GameTraceID != "" {
		attrs = append(attrs, attribute.String(decision.AttrGameTraceID, gc.GameTraceID))
	}
	opts := append([]trace.SpanStartOption{trace.WithAttributes(attrs...)}, gameLink...)
	ctx, span := e.tracer.Start(context.Background(), decision.SpanExperimentEvaluate, opts...)
	defer span.End()

	status, err := e.registry.ConcludeGame(ctx, gc.SessionID, fitness, duration)
	span.SetAttributes(attribute.String(decision.AttrExperimentStatus, string(status)))

	// experiment.verdict (child): record the rolling paired-difference verdict the
	// registry computed, when the conclusion produced one. We READ it back via
	// CompactView — the registry owns the math; this span only surfaces the result.
	if err == nil {
		e.recordVerdictSpan(ctx, gc)
	}
	return status, err
}

// recordVerdictSpan emits the #1140 experiment.verdict span carrying the rolling
// paired-difference verdict (outcome / delta / significance) the registry computed
// for the experiment, read back via CompactView. It is OBSERVABILITY ONLY — it
// computes nothing. The span is emitted only when a verdict is actually present
// (a conclusion that has not yet produced one adds no empty span). The ctx is the
// experiment.evaluate span's context, so the verdict span nests under it.
func (e *experimentLoop) recordVerdictSpan(ctx context.Context, gc gamecontext.GameContext) {
	cv, ok := e.registry.CompactView(gc.ExperimentID)
	if !ok || cv.Verdict == nil {
		return
	}
	v := cv.Verdict
	attrs := []attribute.KeyValue{
		attribute.String(decision.AttrExperimentID, gc.ExperimentID),
		// #1174 consistency: stamp game.id + experiment.arm directly on the verdict span
		// (it previously carried neither) so the verdict is queryable by game and arm
		// like the sibling experiment spans, and findable for "everything for game X".
		attribute.String(decision.AttrGameID, gc.SessionID),
		attribute.String(decision.AttrVerdictOutcome, v.Outcome),
		attribute.Float64(decision.AttrVerdictDelta, v.Delta),
		attribute.Bool(decision.AttrVerdictSignificant, v.Significant),
	}
	if gc.Arm != "" {
		attrs = append(attrs, attribute.String(decision.AttrExperimentArm, gc.Arm))
	}
	_, span := e.tracer.Start(ctx, decision.SpanExperimentVerdict, trace.WithAttributes(attrs...))
	span.End()
}

// experimentInstrumentationName scopes the experiment loop's tracer. It matches the
// decision package's instrumentationName literal so all agent spans share one
// instrumentation scope in Jaeger.
const experimentInstrumentationName = "github.com/joustmania/agent"

// recordFinalizedFitness writes one finalized SHADOW-game fitness sample into the
// store keyed by game_id (#965). It is the capture side the StoreMeasurer reads:
// the same defaultGameFitness scalar ConcludeGame folds into the cohort journal,
// settled (post-withEffectiveDuration), available to the validation path. A nil
// store (test wiring) is a no-op.
func (e *experimentLoop) recordFinalizedFitness(gc gamecontext.GameContext, objective string, fitness, duration float64) {
	if e.fitnessStore == nil {
		return
	}
	if objective == "" {
		objective = decision.ObjectiveBalanced
	}
	e.fitnessStore.Record(experiment.FitnessSample{
		GameID:    gc.SessionID,
		Fitness:   fitness,
		Objective: objective,
		GameKind:  gameKindOrDefault(gc),
		DurationS: duration,
	})
}

// recordFinalizedRealFitness writes a REAL primary game's finalized fitness into
// the store (#1017 §3 item 1) — the previously-DROPPED real-game fitness the
// Validator's Baseline needs. A real game is not bound to one objective, so it is
// scored against EVERY experimentable objective its GameContext can evaluate and a
// per-objective sample is recorded; the StoreBaseline then filters by the
// experiment-under-validation's objective. Only objectives the fitness function
// actually evaluated (signals present) are recorded — a missing-signal objective is
// never fabricated (#731 honesty). A nil store or a non-real game_kind is skipped
// (only real games seed the baseline). A nil store (test wiring) is a no-op.
func (e *experimentLoop) recordFinalizedRealFitness(gc gamecontext.GameContext, duration float64) {
	if e.fitnessStore == nil {
		return
	}
	// Guard the baseline against shadow/unknown-kind games: only a game explicitly
	// labeled real may seed the recent-real baseline. An agent-spawned shadow game
	// never reaches here (it carries an ExperimentID), but a defensively-labeled
	// shadow game with no ExperimentID must not pollute the real baseline.
	if gameKindOrDefault(gc) != experiment.GameKindReal {
		return
	}
	eval := decision.EvaluateFitness(gc, decision.DefaultStaticConfig().FitnessThresholds)
	for objective, r := range eval.Results {
		if !r.Evaluated || !decision.IsExperimentable(objective) {
			continue
		}
		e.fitnessStore.Record(experiment.FitnessSample{
			GameID:    gc.SessionID,
			Fitness:   r.Progress,
			Objective: objective,
			GameKind:  experiment.GameKindReal,
			DurationS: duration,
		})
	}
}

// gameKindOrDefault returns the game's kind label, defaulting an empty label to
// "real": the coordinator labels a player-facing game "real" but an older/edge
// datapoint may omit the label, and the conservative default for the baseline is to
// treat an unlabeled, non-experiment game as real (it has no ExperimentID and was
// not agent-spawned). An agent shadow game ALWAYS carries an ExperimentID, so it is
// never mislabeled real by this default.
func gameKindOrDefault(gc gamecontext.GameContext) string {
	if gc.GameKind == "" {
		return experiment.GameKindReal
	}
	return gc.GameKind
}

// onGameTerminal is the #1014 terminal-outcome backstop, invoked by a spawned
// game's drive goroutine the instant the game ends. For a game that ended WITHOUT
// a usable fitness sample — game_error or force-end (timeout) — it RELEASES the
// in-flight slot directly via the registry (a NON-COUNTING release: no fitness
// sample, no budget increment), so the effective_concurrency=1 loop can never
// deadlock waiting on a telemetry game_active=0 datapoint that may never arrive
// for a failed game. A naturally COMPLETED game is left to the telemetry-driven
// onGameEnd → ConcludeGame path, which folds its real fitness sample; releasing
// it here would drop that sample. ReleaseGame is idempotent, so even if the
// telemetry path raced ahead this is a safe no-op. After a release it nudges
// AllocateAndSpawn so the freed slot is refilled promptly.
func (e *experimentLoop) onGameTerminal(gameID string, outcome gamerunner.Outcome) {
	if outcome == gamerunner.OutcomeCompleted {
		// Natural completion: the telemetry path (onGameEnd → ConcludeGame) concludes
		// it WITH its fitness sample. We deliberately do NOT release here on the
		// HAPPY path because that would drop the sample (ReleaseGame is non-counting).
		//
		// BOUNDED-GRACE BACKSTOP (#1020): the completed-game path trusts the telemetry
		// game_active=0 datapoint to arrive — the same signal #1014 treats as
		// unreliable for ERRORED games. If telemetry DROPS a completed game's
		// datapoint, its in-flight slot would leak forever and the
		// effective_concurrency=1 loop deadlocks (#998). So arm a generous grace
		// timer: if ConcludeGame has NOT folded this game by the time it fires, release
		// the slot non-counting. The grace COMFORTABLY exceeds worst-case telemetry lag
		// (defaultCompletedGrace), so a game that concludes slightly late still folds
		// its sample first — and because ReleaseGame/ConcludeGame are idempotent (both
		// findGameLocked → delete; whoever runs second no-ops), the timer firing AFTER
		// a legitimate conclude is a harmless no-op. The only sacrifice is a real-but-
		// very-late conclude arriving AFTER the grace, which the issue accepts.
		e.armCompletedGrace(gameID)
		return
	}
	// A non-completed terminal outcome supersedes any pending completed-grace timer
	// for this id (defensive: the runner reports one terminal outcome per game, but
	// stop the timer so it can never double-release a since-reused id).
	e.stopCompletedGrace(gameID)
	reason := "game ended without usable fitness sample: " + string(outcome)
	if e.registry.ReleaseGame(gameID, reason) {
		e.log.Warn("experiment: released in-flight slot for non-concluding game (#1014)",
			"game_id", gameID, "outcome", outcome)
		// Refill the freed slot (respecting the kill-switch). context.Background is
		// intentional: this runs on the drive goroutine, which has no tick/request
		// context to thread through — the spawn it triggers is bounded by the runner's
		// own RPC + ready timeouts, so an unbounded Background cannot stall here.
		e.allocateIfEnabled(context.Background())
	}
}

// armCompletedGrace schedules the #1020 bounded-grace backstop for a COMPLETED
// game: after e.completedGrace, if ConcludeGame has not folded the game, release
// its in-flight slot non-counting (no fabricated fitness sample) so the
// effective_concurrency=1 loop cannot deadlock on a dropped game_active=0
// datapoint. The timer is tracked so run() can stop it on shutdown, and is
// self-removing once it fires.
func (e *experimentLoop) armCompletedGrace(gameID string) {
	if e.completedGrace <= 0 {
		return // grace disabled (non-positive) — preserve the pre-#1020 trust-telemetry behavior.
	}
	after := e.afterFunc
	if after == nil {
		after = func(d time.Duration, f func()) func() bool {
			t := time.AfterFunc(d, f)
			return t.Stop
		}
	}
	e.graceMu.Lock()
	if e.graceTimers == nil {
		e.graceTimers = make(map[string]func() bool)
	}
	// Replace any prior timer for this id (a reused/duplicate signal): stop the old
	// one first so only one grace fires per live binding.
	if stop, ok := e.graceTimers[gameID]; ok {
		stop()
	}
	stop := after(e.completedGrace, func() { e.fireCompletedGrace(gameID) })
	e.graceTimers[gameID] = stop
	e.graceMu.Unlock()
}

// fireCompletedGrace is the grace-timer callback: it forgets the timer and, if the
// game is still in-flight (ConcludeGame never folded it — a dropped telemetry
// datapoint), releases its slot non-counting and refills. If ConcludeGame already
// ran, ReleaseGame finds nothing and no-ops (slot freed, sample folded already), so
// a legitimate late conclude is never clobbered — the idempotency the #1019 review
// verified. A fired backstop is logged distinctly so a real telemetry drop is
// OBSERVABLE rather than silent.
func (e *experimentLoop) fireCompletedGrace(gameID string) {
	e.graceMu.Lock()
	delete(e.graceTimers, gameID)
	// Shutdown race guard (#1020): if stopAllCompletedGrace already ran, this timer
	// was firing while run() tore down (its Stop() returned false — too late). Drop
	// the entry and bail WITHOUT ReleaseGame/allocate so we never spawn a shadow game
	// during/after shutdown. Both setter and checker hold graceMu, so this is race-free.
	if e.graceShutdown {
		e.graceMu.Unlock()
		return
	}
	e.graceMu.Unlock()

	reason := "completed game's telemetry game_active=0 datapoint never arrived (#1020 grace backstop)"
	if e.registry.ReleaseGame(gameID, reason) {
		// The slot was STILL in-flight after the full grace ⇒ the completed game's
		// conclude datapoint was genuinely dropped. Audit it at Warn (distinct from the
		// normal conclude path) so the drop is visible in the agent's logs/metrics.
		e.log.Warn("experiment: completed-game grace backstop fired — released leaked slot (#1020)",
			"game_id", gameID, "grace", e.completedGrace)
		// Refill the freed slot (respecting the kill-switch). context.Background is
		// intentional here for the same reason as the #1014 release path: this runs on
		// a runtime timer goroutine with no request/tick context to thread.
		e.allocateIfEnabled(context.Background())
	}
	// else: ConcludeGame already folded the game — the happy path won the race and the
	// backstop is a silent no-op (no slot freed, sample already counted).
}

// stopCompletedGrace cancels and forgets a pending completed-grace timer for
// gameID, if any. It is idempotent (a missing entry is a no-op).
func (e *experimentLoop) stopCompletedGrace(gameID string) {
	e.graceMu.Lock()
	if stop, ok := e.graceTimers[gameID]; ok {
		stop()
		delete(e.graceTimers, gameID)
	}
	e.graceMu.Unlock()
}

// stopAllCompletedGrace cancels every pending completed-grace timer (shutdown
// teardown), so no timer fires after the loop returns. Called from run() on ctx
// cancellation alongside AbortAll.
func (e *experimentLoop) stopAllCompletedGrace() {
	e.graceMu.Lock()
	// Latch shutdown FIRST: any callback already firing (whose Stop() returns false)
	// will observe this under graceMu in fireCompletedGrace and bail without spawning.
	e.graceShutdown = true
	for id, stop := range e.graceTimers {
		stop()
		delete(e.graceTimers, id)
	}
	e.graceMu.Unlock()
}

// defaultGameFitness scores a finished game into one fitness scalar in [0,1] using
// the M7 fitness engine (#731): it evaluates the experiment's objective against the
// game-end GameContext and returns that objective's Progress (1.0 = the objective
// was fully satisfied, 0.0 = maximally failing). Higher = better, the convention
// the CohortVerdict and Promoter already use. An unknown/empty objective falls back
// to "balanced"; an objective whose signals were not observed returns 0 (a game
// that produced no measurable signal contributes a worst-case sample rather than a
// fabricated one). This is deliberately the SIMPLEST honest mapping — a richer
// blended fitness is a follow-up (it does not change the assembly).
func defaultGameFitness(gc gamecontext.GameContext, objective string) float64 {
	if objective == "" {
		objective = decision.ObjectiveBalanced
	}
	eval := decision.EvaluateFitness(gc, decision.DefaultStaticConfig().FitnessThresholds)
	if r, ok := eval.Results[objective]; ok && r.Evaluated {
		return r.Progress
	}
	return 0
}

// withEffectiveDuration returns gc with Session.DurationSeconds populated from the
// partition's start→end phase timeline when the coordinator's end-only
// game_duration_seconds gauge has not yet been observed (DurationSeconds == nil).
// It is a no-op when the gauge IS present (the gauge is authoritative) or when the
// timeline lacks a start/end pair to bracket (no signal to recover — a genuinely
// signal-less game still folds the worst-case 0, the #994 contract).
//
// This is the honest #997 fix: the recovered value is the real wall-clock game
// duration the agent's own Store stamped on the session phase events
// (SetGameActive start/end), NOT a fabricated number — so duration-based
// objectives (endurance/accelerate) get the signal they need at conclusion.
func withEffectiveDuration(gc gamecontext.GameContext) gamecontext.GameContext {
	if gc.Session.DurationSeconds != nil {
		return gc
	}
	dur, ok := timelinePhaseDuration(gc.Timeline)
	if !ok {
		return gc
	}
	gc.Session.DurationSeconds = &dur
	return gc
}

// timelinePhaseDuration derives the elapsed game time from a partition's rolling
// narrative: the seconds between the session's "start" phase event and its "end"
// phase event. It returns false when either bracket is missing or the span is
// non-positive (nothing trustworthy to recover). The end snapshot the Store hands
// OnGameEnd is taken right AFTER appending the "end" phase (store.go SetGameActive),
// so both brackets are present on a normally concluded game.
func timelinePhaseDuration(timeline []gamecontext.TimelineEvent) (float64, bool) {
	var start, end time.Time
	var haveStart, haveEnd bool
	for _, ev := range timeline {
		if ev.Kind != gamecontext.EventPhase {
			continue
		}
		switch ev.Detail {
		case "start":
			start = ev.At
			haveStart = true
		case "end":
			end = ev.At
			haveEnd = true
		}
	}
	if !haveStart || !haveEnd {
		return 0, false
	}
	d := end.Sub(start).Seconds()
	if d <= 0 {
		return 0, false
	}
	return d, true
}

// experimentDefaultsFromEnv resolves the BOOTSTRAP defaults for the eight migrated
// runtime knobs (#1044) from the AGENT_EXPERIMENT_* env vars (#1041/#1043). These
// become the per-tick fallback floor the live agent.json flags override: a fresh
// agent.json (no new flags) reproduces byte-identical env behavior, and an
// unreachable flagd falls back to exactly these values. It reuses the SAME env
// resolvers the registry/verdict already own (no duplicate parsing), so the
// bootstrap layer stays coherent with #1041/#1043.
func experimentDefaultsFromEnv() flags.ExperimentDefaults {
	return flags.ExperimentDefaults{
		Enabled:                    experimentsEnabled(),
		DynamicEnabled:             dynamicEnabled(),
		Tick:                       experimentTick(),
		DynamicMaxConcurrent:       dynamicMaxConcurrent(),
		VerdictMinN:                experiment.MinNFromEnv(),
		VerdictMinPairs:            experiment.MinPairsFromEnv(),
		MaxGames:                   experiment.MaxGamesPerExperimentFromEnv(),
		ShadowEffectiveConcurrency: experiment.EffectiveConcurrencyFromEnv(),
	}
}

// experimentTick resolves the AllocateAndSpawn cadence from
// AGENT_EXPERIMENT_TICK_SECONDS, falling back to defaultExperimentTick on an
// unset/invalid/non-positive value.
func experimentTick() time.Duration {
	if v := strings.TrimSpace(os.Getenv(envExperimentTick)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return defaultExperimentTick
}

// completedGrace resolves the #1020 bounded-grace window from
// AGENT_EXPERIMENT_COMPLETED_GRACE_SECONDS, falling back to defaultCompletedGrace
// on an unset/invalid value. A value of 0 (or negative) explicitly DISABLES the
// backstop — restoring the pre-#1020 trust-telemetry behavior — which is why a
// non-positive override is honored verbatim rather than coerced to the default.
func completedGrace() time.Duration {
	if v := strings.TrimSpace(os.Getenv(envCompletedGrace)); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return time.Duration(n) * time.Second
		}
	}
	return defaultCompletedGrace
}

// dynamicMaxConcurrent resolves the dynamic-declarer concurrent-experiment
// backstop from AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT, falling back to
// defaultDynamicMaxConcurrent on an unset/invalid/non-positive value (a
// zero/negative cap would let the declarer either never declare or be unbounded,
// so it is treated as misconfiguration). It is further clamped to the shadow
// capacity in declareDynamicIfEnabled.
func dynamicMaxConcurrent() int {
	if v := strings.TrimSpace(os.Getenv(envDynamicMaxConcurrent)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return defaultDynamicMaxConcurrent
}

// M7-7 validation seam env knobs (#965). These tune the LIVE Validator wiring; all
// have safe defaults so the validation path is wired even with none set. They are
// distinct from the cohort knobs (#1044) because the synthetic-validation gate is a
// separate, gated path (the autonomous-promotion #1016 surface).
const (
	// envFitnessStoreCapacity overrides the finalized-fitness ring size.
	envFitnessStoreCapacity = "AGENT_FITNESS_STORE_CAPACITY"
	// envValidationGameTimeout overrides the per-cycle await for shadow games' fitness.
	envValidationGameTimeout = "AGENT_VALIDATION_GAME_TIMEOUT_SECONDS"
	// envValidationBaselineRecentN overrides how many recent real games the baseline averages.
	envValidationBaselineRecentN = "AGENT_VALIDATION_BASELINE_RECENT_N"
	// envValidationBaselineMinSamples overrides the minimum real samples a baseline needs.
	envValidationBaselineMinSamples = "AGENT_VALIDATION_BASELINE_MIN_SAMPLES"
)

// defaultValidationBaselineRecentN is the default number of recent real games the
// StoreBaseline averages. It mirrors the verdict's DefaultMinNPerArm (8) so the
// baseline rests on a comparable sample to what the cohort verdict already trusts
// (#1017 §2).
const defaultValidationBaselineRecentN = 8

// defaultValidationBaselineMinSamples is the default minimum real samples before a
// baseline is trusted. Below it the Validator FAILS CLOSED (no promotion against a
// baseline of too-few real games). A conservative floor of 3 means a baseline is
// never a single-game fluke.
const defaultValidationBaselineMinSamples = 3

// fitnessStoreCapacity resolves the finalized-fitness ring size from
// AGENT_FITNESS_STORE_CAPACITY, falling back to the package default on an
// unset/invalid/non-positive value.
func fitnessStoreCapacity() int {
	return positiveIntEnv(envFitnessStoreCapacity, experiment.DefaultFitnessStoreCapacity)
}

// validationGameTimeout resolves the per-cycle await timeout from
// AGENT_VALIDATION_GAME_TIMEOUT_SECONDS (seconds), falling back to
// DefaultValidationGameTimeout on an unset/invalid/non-positive value.
func validationGameTimeout() time.Duration {
	if v := strings.TrimSpace(os.Getenv(envValidationGameTimeout)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
	}
	return DefaultValidationGameTimeout
}

// recentRealBaselineAccessor adapts the #965 FitnessStore into the verdict's #992
// RecentRealBaseline seam: given an objective it returns the mean finalized fitness
// of the last recentN REAL games and whether a TRUSTWORTHY baseline exists (>=
// minSamples real samples). It returns ok=false (anchor SKIPPED — fail open) when
// the store is unwired or too few real samples exist, mirroring StoreBaseline's
// fail-closed-for-promotion threshold but expressed as the anchor's fail-OPEN bool.
// store may be nil ⇒ the accessor always reports ok=false (anchor disabled).
func recentRealBaselineAccessor(store *experiment.FitnessStore, recentN, minSamples int) experiment.RecentRealBaseline {
	if recentN < 1 {
		recentN = 1
	}
	if minSamples < 1 {
		minSamples = 1
	}
	return func(objective string) (float64, bool) {
		if store == nil {
			return 0, false
		}
		samples := store.RecentReal(objective, recentN)
		if len(samples) < minSamples {
			return 0, false
		}
		var sum float64
		for _, s := range samples {
			sum += s.Fitness
		}
		return sum / float64(len(samples)), true
	}
}

// validationBaselineRecentN resolves how many recent real games the baseline
// averages from AGENT_VALIDATION_BASELINE_RECENT_N.
func validationBaselineRecentN() int {
	return positiveIntEnv(envValidationBaselineRecentN, defaultValidationBaselineRecentN)
}

// validationBaselineMinSamples resolves the minimum real samples a baseline needs
// from AGENT_VALIDATION_BASELINE_MIN_SAMPLES.
func validationBaselineMinSamples() int {
	return positiveIntEnv(envValidationBaselineMinSamples, defaultValidationBaselineMinSamples)
}

// positiveIntEnv parses a positive integer env var, returning fallback on an
// unset/invalid/non-positive value.
func positiveIntEnv(key string, fallback int) int {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return fallback
}

// validatorObjectiveResolver maps a Proposal to its experiment's fitness objective
// by reading the registry's CompactView keyed by the proposal's ExperimentID. The
// objective is what the StoreBaseline filters recent-real samples by, so the
// baseline is per-objective (#1017 §3 item 4). An unknown experiment id yields ""
// (any-objective), the safe degrade.
func validatorObjectiveResolver(reg *experiment.Registry) experiment.ObjectiveResolver {
	return func(p experiment.Proposal) string {
		if reg == nil || p.ExperimentID == "" {
			return ""
		}
		if cv, ok := reg.CompactView(p.ExperimentID); ok {
			return cv.Intent.Objective
		}
		return ""
	}
}

// seedIntentFromEnv builds the single seed experiment Intent from the
// AGENT_EXPERIMENT_SEED_* env vars, or returns nil when no seed flag is set. This
// is the SIMPLEST env-gated declaration trigger (#991 investigation): rather than
// wiring the LLM Proposer (#960) — which produces single-flag Proposals, not
// experiment-shaped Intents with arms/target-N, and is itself inert until a real
// inference backend lands — a config-seeded experiment lets the cohort loop run
// end-to-end for the demo behind the same opt-in. Promoting the Proposer to emit
// Intents is a documented follow-up.
func seedIntentFromEnv() *experiment.Intent {
	flagKey := strings.TrimSpace(os.Getenv(envSeedFlag))
	if flagKey == "" {
		return nil
	}
	objective := strings.TrimSpace(os.Getenv(envSeedObjective))
	if objective == "" {
		objective = decision.ObjectiveBalanced
	}
	targetN := 0
	if v := strings.TrimSpace(os.Getenv(envSeedTargetN)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			targetN = n
		}
	}
	hypothesis := strings.TrimSpace(os.Getenv(envSeedHypothesis))
	if hypothesis == "" {
		hypothesis = "env-seeded experiment (#991 demo trigger)"
	}
	return &experiment.Intent{
		Hypothesis:        hypothesis,
		FlagKey:           flagKey,
		ExperimentalValue: parseSeedValue(os.Getenv(envSeedValue)),
		Objective:         objective,
		TargetNPerArm:     targetN,
	}
}

// parseSeedValue parses the seed experimental value: a number when it parses as a
// float, a bool for "true"/"false", a JSON object/array when the raw text begins
// with '{' or '[' and parses as valid JSON (so an OBJECT-valued game flag — e.g.
// the FFA difficulty/pacing levers “game.thresholds“ / “game.windows“ — can be
// seeded, #1090), else the raw string. The Gate's type guard rejects a value whose
// JSON kind does not match the flag's existing variants, so a mistyped seed fails
// closed at Start (the targeting write) — never reaching a shadow game.
func parseSeedValue(raw string) any {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if f, err := strconv.ParseFloat(raw, 64); err == nil {
		return f
	}
	if b, err := strconv.ParseBool(raw); err == nil {
		return b
	}
	// Structured (object/array) seed: only attempt JSON when the text is clearly
	// a JSON composite, so a bare string seed is never accidentally reinterpreted.
	if len(raw) > 0 && (raw[0] == '{' || raw[0] == '[') {
		var v any
		if err := json.Unmarshal([]byte(raw), &v); err == nil {
			return v
		}
	}
	return raw
}
