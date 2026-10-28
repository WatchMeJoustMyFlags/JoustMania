package main

import (
	"context"
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
	"github.com/joustmania/agent/patterngame"
	"github.com/joustmania/agent/promote"
)

// experiment_loop.go is the FINAL ASSEMBLY of the experiment / cohort framework
// (#991, epic #982): it constructs the experiment.Registry with the REAL injected
// seams and runs the cohort loop inside the agent — declare an experiment, spawn
// shadow games into its arms, fold their conclusions into the journal, reach a
// verdict, and (gated) route a promote verdict to the #961 Promoter.
//
// OVERRIDING SAFETY (#991/#1044): the ENTIRE loop is gated behind the LIVE
// experiments_enabled flag (default off, and off while it cannot be resolved; no env
// fallback since #1907) AND the agent kill-switch (agent.json `enabled`). Since #1044 the loop is
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
	// envDynamicMaxConcurrent is the operator's CEILING on the number of LIVE
	// experiments the declarers keep going at once (#1810, C-034, FR-152 e): the
	// effective budget is min(env, experiment_dynamic_max_concurrent), clamped to
	// the shadow-game capacity (concurrentBudget). Unset, invalid or non-positive ⇒
	// no ceiling (the flag governs).
	envDynamicMaxConcurrent = "AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT"

	// envMaxGames is the operator's CEILING on the per-experiment games budget
	// (#1810, C-034, FR-152 f): effective = min(env, experiment_max_games), where 0
	// or less means "no limit" on either side. Unset, invalid or non-positive ⇒ no
	// ceiling (the flag governs).
	envMaxGames = "AGENT_EXPERIMENT_MAX_GAMES"

	// envProposerEnabled is the opt-in for the retro-seeded THESIS proposer (#1184,
	// epic #1181 incr. 4): the seam that turns post-game retro suggestions (#1179)
	// into candidate experiment theses, closing the game→retro→thesis→experiment loop.
	// Default false ⇒ recorded retro suggestions are NEVER turned into experiments,
	// byte-identical to pre-#1184 declaration behavior. Subordinate to
	// the experiments_enabled flag + the agent.json kill-switch (it is only consulted from
	// allocateIfEnabled, which self-aborts when either is off) and, like the #995
	// declarer, it reuses the SAME bounded flag selection + Gate + Registry caps — it
	// adds ONLY the thesis provenance, never a new write surface.
	//
	// #1810 (C-034, FR-152 d): the env is the operator's CEILING of the proposer
	// gate; the gate is open only when it is true AND the human-owned
	// experiment_proposer_enabled flag is on (proposerGateOpen). The same gate also
	// governs the M7-4 proposer (main.go proposerPermit).
	envProposerEnabled = "AGENT_EXPERIMENT_PROPOSER_ENABLED"
)

// proposerEnabled reports the env half of the proposer gate (#1184, #1810): the
// operator's ceiling AGENT_EXPERIMENT_PROPOSER_ENABLED. Unset = closed.
func proposerEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(envProposerEnabled)), "true")
}

// proposerGateOpen is the proposer gate (#1810, C-034, FR-152 d): the env ceiling
// AND the live experiment_proposer_enabled flag. The flag reads false when it
// cannot be resolved, so a lost sync closes the gate.
func proposerGateOpen(flagOn bool) bool {
	return proposerEnabled() && flagOn
}

// defaultDynamicMaxConcurrent is the default ceiling on simultaneously-live
// dynamically-declared experiments (#995). A small value keeps the autonomous
// declarer from flooding the registry; it is further clamped to the shadow-game
// capacity. The experiment seed (if any) counts toward this ceiling too — the
// declarer reads the registry's LIVE count, which includes the seed.
const defaultDynamicMaxConcurrent = 3

// defaultExperimentTick is the AllocateAndSpawn cadence when unset. Shadow games
// are minutes-long, so a 30s refill tick is ample headroom over game duration.
const defaultExperimentTick = 30 * time.Second

// firstTickCap bounds the interval to the SECOND experiment tick (#1907). run()
// takes its first interval before flagd has synced, so it would otherwise always be
// the compiled 30 s default, even when experiment_tick_seconds is lower. Capping it
// costs at most one extra gated tick (inert when a gate is closed); after that tick
// the re-arm moves the ticker to the live cadence.
const firstTickCap = 5 * time.Second

// loopTicker is the ticker run() refills on; *time.Ticker satisfies it via
// timeTicker.
type loopTicker interface {
	Chan() <-chan time.Time
	Reset(d time.Duration)
	Stop()
}

// timeTicker adapts *time.Ticker to loopTicker.
type timeTicker struct{ *time.Ticker }

func (t timeTicker) Chan() <-chan time.Time { return t.C }

// defaultCompletedGrace is the bounded-grace window (#1020) for a COMPLETED game
// whose telemetry game_active=0 datapoint may never arrive. It must COMFORTABLY
// exceed the worst-case telemetry-arrival lag so a game that concludes slightly
// late is still CONCLUDED with its real fitness sample (the grace fires only after
// ConcludeGame has had ample time and still has not folded the game). The
// game-end datapoint is emitted at session teardown and folded by onGameEnd within
// a handful of seconds in practice; 60s is an order of magnitude over that typical
// conclude latency yet still bounded — far short of leaking the slot "forever",
// which is the failure this backstop removes. Tunable via the
// experiment_completed_grace_seconds flag (this constant is its default).
const defaultCompletedGrace = 60 * time.Second

// parkFloor is the shortest time a shadow game's telemetry end waits for the
// runner's terminal outcome (UC-046 BR-016, #1405). The runner reports an
// outcome-less game_ended only after DefaultEndedGrace, and it reports every
// outcome after one controller-removal RPC (RPCTimeout), so a shorter wait would
// release a natural game as unclassified. The park window is
// max(experiment_completed_grace_seconds, parkFloor), so grace 0 (off) never
// parks forever and never drops a natural sample.
const parkFloor = gamerunner.DefaultEndedGrace + gamerunner.DefaultRPCTimeout + 5*time.Second

// releaseReasonNoOutcome is the ReleaseGame reason of a parked telemetry end
// whose runner outcome never arrived (#1405). It fails closed: no sample. It is
// neither the preemption nor the stale reason, so it counts against the pair's
// 3-rerun budget (BR-015) like any other release.
const releaseReasonNoOutcome = "game ended without usable fitness sample: no terminal outcome"

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
	// BOTH a shadow experiment game and a real primary game (averaged by the #992
	// recent-real anchor and the autonomous Watcher). nil ⇒ recording is skipped
	// (test wiring that does not exercise the fitness path).
	fitnessStore *experiment.FitnessStore

	// expDefaults are the COMPILED defaults for the migrated runtime knobs (#1044,
	// #1907): each tick's live config(ctx) read falls back to these when the matching
	// agent.json flag is absent/unreadable. None is env-derived, and the two gates
	// have no default here (they fall back to false).
	expDefaults flags.ExperimentDefaults

	// seedLast is the canonical JSON of the last experiment_seed value this
	// process acted on (declared, skipped as satisfied, refused or malformed);
	// seedLatched says whether it is set. Observing `off` clears both
	// (experiment_seed.go, #1907). Guarded by declarerLock().
	seedLast    string
	seedLatched bool
	// seedMu is declarerLock() when no dynamic declarer is wired (test wiring).
	seedMu sync.Mutex

	// dynamic is the heuristic dynamic declarer (#995). #1044: it is ALWAYS built
	// now; whether it actually declares each tick is gated by the LIVE
	// experiment_dynamic_enabled flag (checked in declareDynamicIfEnabled), so an
	// off→on flip starts dynamic declaration with no restart. nil only in tests that
	// do not wire it.
	dynamic *dynamicDeclarer

	// proposer is the retro-seeded THESIS proposer (#1184): it consumes the post-game
	// retro's structured suggestions (#1179) and turns the oldest into a candidate
	// experiment whose thesis is seeded from the retro's reasoning, closing the
	// game→retro→thesis→experiment loop. It DELEGATES flag/value selection to e.dynamic
	// (the proven #995 selector), adding only the thesis provenance + objective bias.
	// Whether it actually declares each tick is gated by proposerEnabled() (default
	// OFF, #1184). nil only in tests that do not wire it. The retro coordinator feeds it
	// via Record (wired in main.go).
	proposer *experiment.ThesisProposer

	// configOverride, when non-nil, supplies the live ExperimentConfig instead of
	// reading e.flags.Experiment — the injectable seam tests use to drive the
	// startup-gate inversion (off→on→off) and the per-knob live reads without a flagd
	// dependency. Production leaves it nil (the real flags client is consulted).
	configOverride func(ctx context.Context) flags.ExperimentConfig

	// startMu guards startedOnce so the deferred bootstrap runs EXACTLY once even
	// though allocateIfEnabled is called concurrently from several goroutines (tick,
	// immediate allocate, per-partition onGameEnd, onGameTerminal, grace timer).
	startMu sync.Mutex
	// startedOnce guards the one-time rehydrate that runs the FIRST time the loop
	// observes experiments_enabled=true (the startup-gate inversion, #1044): the
	// loop is always running, but its bootstrap is deferred until the flag turns
	// on, so an agent that starts with experiments off and is flipped on later
	// still rehydrates prior experiments. The seed is not part of it any more: it
	// is read every enabled tick (maybeDeclareSeed, #1907).
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

	// thresholds supplies the fitness.* thresholds real-game fitness is scored
	// against, read at each game end (UC-043 step 4/7, D-F5, #1740). nil uses the
	// flagd-schema defaults (test wiring); production reads the live agent flags.
	thresholds decision.FitnessSource

	// promotions reverts the autonomous promotions this agent run applied when the
	// kill switch is off (UC-043 A1, UC-045 step 5, #1704). nil: nothing to revert.
	promotions promotionKillSwitch
	// deferred drains the promoter's queue of hold-key real writes and reverts
	// every promote.DeferredDrainInterval with the live promotionConfig (UC-043 A17
	// step 3); nil = no queue (test wiring).
	deferred        deferredPromotions
	promotionConfig promote.ConfigResolver

	// ceilings are the operator's env ceilings on the widening experiment settings
	// (#1810, C-034): read once at startup (the process env does not change); 0 =
	// no ceiling. Tests leave them zero.
	ceilings experimentCeilings

	// afterFunc schedules f to run after d, returning a stop func (true if it
	// cancelled a not-yet-fired timer). Defaults to a time.AfterFunc adapter;
	// tests inject a fake clock so the grace can fire without a real sleep.
	afterFunc func(d time.Duration, f func()) (stop func() bool)

	// newTicker builds run()'s refill ticker. nil ⇒ a time.Ticker; tests inject a
	// fake so the first-tick cap and the re-arm are observable without real sleeps.
	newTicker func(d time.Duration) loopTicker
	// newHeartbeat builds run()'s experiment.progress heartbeat ticker (#2059,
	// experiment.ProgressHeartbeatInterval). nil ⇒ a time.Ticker; tests inject a fake.
	newHeartbeat func(d time.Duration) loopTicker

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

	// parked and outcomes join the two end signals of a bound experiment game
	// (UC-046 BR-016, #1405), both guarded by graceMu. parked holds a telemetry end
	// (its GameContext snapshot) that arrived before the runner's terminal outcome;
	// outcomes holds a terminal outcome that arrived before the telemetry end. A
	// sample is folded only when both are in and the outcome is a sample outcome.
	// Each entry has one timer in graceTimers that bounds it.
	parked   map[string]gamecontext.GameContext
	outcomes map[string]gamerunner.Outcome
	// hold is the UC-043 A17 gate of the declarer; main shares its policy with the
	// proposer vocabulary.
	hold *holdGate
}

// gameFitnessFunc scores a finished game (its pre-reset GameContext) into one
// fitness scalar in [0,1] (higher = better), the per-game sample the cohort
// aggregator folds. The default (defaultGameFitness) reuses the M7 fitness engine.
type gameFitnessFunc func(gc gamecontext.GameContext, objective string) float64

// crossGameWindow is the part of the cross-game window (gamewindow.Store) the
// experiment loop feeds: finished pattern games add their per-slot rows to their
// experiment's linked set (UC-042 BR-006), and a torn-down experiment's linked set
// is evicted (UC-042 BR-005).
type crossGameWindow interface {
	RecordPattern(patterngame.Record)
	Forget(experimentID string)
}

// patternEndChain returns the spawner's pattern-end callback: the registry's
// replay-record hook first (journal + span event), then the window sink when one
// is wired.
func patternEndChain(registryEnd gamerunner.PatternCallback, sink func(patterngame.Record)) gamerunner.PatternCallback {
	if sink == nil {
		return registryEnd
	}
	return func(rec patterngame.Record) {
		registryEnd(rec)
		sink(rec)
	}
}

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
//   - CohortVerdict   → experiment.NewDefaultVerdict() (#979 min-N + effect-size).
//   - Promoter        → promote.NewExperimentPromoter(promoter, resolver) (#980 over
//     the #961 Promoter), resolver reading LIVE flags.Promotion + the kill-switch.
//   - ShadowSpawner   → the real outbound game-coordinator client (shadowSpawner).
func buildExperimentLoop(
	flagsClient *flags.Flags,
	promoter *promote.Promoter,
	fitnessStore *experiment.FitnessStore,
	gameFlags experiment.FlagService,
	window crossGameWindow,
	logger *slog.Logger,
) *experimentLoop {
	// #1044 startup-gate inversion: the loop is ALWAYS built now (no nil return).
	// experiments_enabled is a LIVE agent.json flag, so the loop must exist + run so
	// an off→on flip at runtime starts it with no restart. It SELF-GATES each tick on
	// the live flag (allocateIfEnabled / run): while disabled it does nothing —
	// rehydrate+seed are deferred (startedOnce) and AllocateAndSpawn is skipped — so
	// the disabled agent is behaviorally inert (it runs one no-op tick goroutine but
	// spawns/writes/promotes nothing). An unresolved experiments_enabled reads false
	// (#1907: no env fallback).
	expDefaults := experimentDefaults()

	logger.Info("Experiment cohort loop constructed (#991/#1044) — gated LIVE by agent.json experiments_enabled",
		"tick_default", expDefaults.Tick,
		"completed_grace_default", expDefaults.CompletedGrace,
		"max_shadow_games", experiment.DefaultMaxShadowGames,
		"effective_concurrency_default", expDefaults.ShadowEffectiveConcurrency)

	// TargetingWriter (#977): a Gate over a Writer pinned to the game flagset. The
	// Gate validates THE INVARIANT on every Apply; Strip un-nests a branch on
	// teardown. The Writer is shared by Gate (Apply) and the adapter (Strip).
	writer := experiment.NewWriter(gameFlags, logger)
	gate := experiment.NewGate(writer, nil, logger)
	targeting := experiment.NewGateTargetingWriter(gate, writer)

	// Promoter (#980/#961): adapt the already-built, env-gated #961 Promoter. The
	// ConfigResolver reads the LIVE promotion mode/target AND the agent kill-switch
	// each promotion, so the real-default path stays behind every existing gate.
	promo := promote.NewExperimentPromoter(promoter, promotionConfigResolver(flagsClient))

	// ShadowSpawner (#976): the real outbound game-coordinator client.
	spawner := newShadowSpawner(gamerunner.ConfigFromEnv(), logger)

	// FitnessStore (#965/#1017): the finalized-per-game fitness store onGameEnd
	// Records into and the #992 recent-real anchor reads from. It is built
	// by the CALLER (main.go) and passed in so the SAME store also backs the autonomous
	// Watcher's real-game fitness source (#1057). A nil store (a test or a caller that
	// did not build one) falls back to a fresh local store so the loop is still valid.
	if fitnessStore == nil {
		fitnessStore = newFitnessStore()
	}

	// CohortVerdict (#979) + recent-real SECONDARY anchor (#992): the within-experiment
	// comparison gated by recent REAL play. The anchor reads the FitnessStore's
	// RecentReal mean for the experiment's objective via a nil-safe accessor; it fails
	// OPEN when no real-game baseline exists (the current mock/shadow-only reality), so
	// it never blocks the shadow loop.
	verdict := experiment.NewDefaultVerdict().WithRecentRealBaseline(
		recentRealBaselineAccessor(fitnessStore, anchorBaselineRecentN, anchorBaselineMinSamples),
		experiment.DefaultAnchorMargin,
	)

	// UC-046: experiments are archetype-only; the pinner resolves the selection in
	// each experiment's first allocation pass.
	pinner := &flagPatternPinner{
		flags: flagsClient,
		mode:  spawner.spec.GameName,
		minPairs: func(ctx context.Context) int {
			return flagsClient.Experiment(ctx, expDefaults).VerdictMinPairs
		},
		log: logger,
	}
	registry := experiment.NewRegistry(experiment.RegistryConfig{
		Root:      journal.DirFromEnv(),
		Spawner:   spawner,
		Verdict:   verdict,
		Promoter:  promo,
		Targeting: targeting,
		Log:       logger,
		// UC-043 A17 step 3: a flag whose deferred promotion waits counts as under test.
		PendingPromotions: pendingPromotionFlags(promoter),
		// UC-046: pin experiment.mock_archetypes at each experiment's first spawn;
		// the kill switch force-ends in-flight pattern games through the tracker.
		Patterns: pinner,
		// UC-043 A17 step 2: hold-flag experiments are checked at the pin and per tick.
		HoldGate:       registryHoldGate(gameFlags),
		FlagDigest:     resolvedFlagDigest,
		PatternTracker: patterngame.Default,
	})
	var windowSink func(patterngame.Record)
	if window != nil {
		windowSink = window.RecordPattern
		// UC-042 BR-005: a torn-down experiment's linked shadow history is evicted.
		registry.SetTerminalHook(window.Forget)
	}
	spawner.SetPatternCallbacks(registry.RecordPatternStart, patternEndChain(registry.RecordPatternEnd, windowSink))

	// UC-043 A17 step 1: the declarer offers a hold key only while the selected
	// archetypes all press the trigger in a Free-for-All shadow mode.
	hold := &holdGate{}
	hold.setPolicy(func(ctx context.Context) bool {
		pin := pinner.Pin(ctx)
		return pin.Active() && len(experiment.ArchetypesWithoutTriggerPolicy(pin)) == 0
	})

	thresholds := flagsFitnessSource{flags: flagsClient}
	loop := &experimentLoop{
		registry:     registry,
		spawner:      spawner,
		flags:        flagsClient,
		log:          logger,
		tracer:       otel.Tracer(experimentInstrumentationName),
		fitnessStore: fitnessStore,
		expDefaults:  expDefaults,
		fitness:      gameFitnessWith(thresholds),
		thresholds:   thresholds,
		ceilings:     experimentCeilingsFromEnv(logger),
		dynamic:      newDynamicDeclarer(gameFlags, nil),
		hold:         hold,
		// #1184: the retro-seeded thesis proposer is ALWAYS built (like the #995
		// declarer) so the retro coordinator can Record into it unconditionally; whether
		// it actually DECLARES is gated by the proposer gate (env AND flag, #1810) in
		// declareFromRetroIfEnabled. Backlog is bounded (DefaultMaxRetroBacklog).
		proposer: experiment.NewThesisProposer(0),
	}
	if promoter != nil {
		loop.promotions = promoter
		// A re-watch revert journals on its experiment (UC-043 step 7, #1883).
		promoter.SetRewatchJournal(loop.journalRewatchRevert)
		// UC-043 A17 step 3: the deferred real writes and reverts of hold keys.
		loop.deferred = promoter
		loop.promotionConfig = promotionConfigResolver(flagsClient)
		if !promoter.HasRealGameProbe() {
			logger.Warn("code_improvement: no real-game probe wired; every autonomous promotion of a hold key waits forever (UC-043 A17, fail closed)")
		}
	}
	loop.dynamic.hold = hold
	logger.Info("Dynamic experiment declarer constructed (#995/#1044) — gated LIVE by agent.json experiment_dynamic_enabled; settings are the experiment_dynamic_* flags, read per tick (#1907)")
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
//   - ENABLED ⇒ apply the live caps (Reconfigure); on the FIRST enabled tick run
//     the one-time rehydrate (#831 durable resume, startedOnce); declare the
//     experiment seed if its value is new (#1907); then refill shadow capacity
//     (dynamic declare + allocate). An
//     off→on flip therefore STARTS the loop (rehydrate+seed+allocate) and an on→off
//     flip STOPS it cleanly (AbortAll strips targeting + releases capacity; in-flight
//     shadow games conclude/release normally — no corruption).
//
// The tick CADENCE itself is live (experiment_tick_seconds): the ticker is re-armed
// whenever the resolved interval moves, mirroring the #927 eviction-ticker pattern,
// so retuning the cadence takes effect with no restart.
//
// SEED + DYNAMIC COMPOSITION (#995, #1907): the experiment_seed flag is read every
// enabled tick and each distinct value is acted on at most once per process
// (maybeDeclareSeed); the dynamic declarer then fills the rest each tick,
// bounded by the live concurrent-experiment backstop. The kill-switch (agent.json
// `enabled`) is ALSO honored each tick (agentEnabled), independent of
// experiments_enabled — either being off tears the surface down.
func (e *experimentLoop) run(ctx context.Context) {
	// First action immediately so an already-enabled agent accrues games without
	// waiting a full tick (and a disabled one tears down promptly), then refill on
	// the live cadence. The first interval is read before flagd has synced, so it is
	// capped at firstTickCap (#1907); the re-arm below then moves to the live value.
	interval := min(e.tickInterval(ctx), firstTickCap)
	e.allocateIfEnabled(ctx)

	newTicker := e.newTicker
	if newTicker == nil {
		newTicker = func(d time.Duration) loopTicker { return timeTicker{time.NewTicker(d)} }
	}
	ticker := newTicker(interval)
	defer ticker.Stop()
	// #2059: every live experiment re-emits its experiment.progress snapshot on a
	// fixed cadence, independent of the refill tick, so an idle one stays visible.
	newHeartbeat := e.newHeartbeat
	if newHeartbeat == nil {
		newHeartbeat = func(d time.Duration) loopTicker { return timeTicker{time.NewTicker(d)} }
	}
	heartbeat := newHeartbeat(experiment.ProgressHeartbeatInterval)
	defer heartbeat.Stop()
	// UC-043 A17 step 3: drain the deferred hold-key writes and reverts.
	var drain <-chan time.Time
	if e.deferred != nil {
		drainTicker := time.NewTicker(promote.DeferredDrainInterval)
		defer drainTicker.Stop()
		drain = drainTicker.C
	}
	for {
		select {
		case <-heartbeat.Chan():
			e.registry.ProgressHeartbeat()
		case <-drain:
			e.drainDeferred(ctx)
		case <-ctx.Done():
			// UC-043 A17: journal the dropped deferred writes before the journal closes.
			// A waiting revert runs now if no real game is live, else it stays journaled.
			if e.deferred != nil {
				dctx, dcancel := context.WithTimeout(context.Background(), 5*time.Second)
				e.deferred.DropDeferred(dctx, promote.ReasonShutdown)
				dcancel()
			}
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
		case <-ticker.Chan():
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
// flags.Experiment with the compiled defaults as the fallback. A nil flags client
// (test wiring only) yields the compiled defaults with both gates closed.
func (e *experimentLoop) config(ctx context.Context) flags.ExperimentConfig {
	if e.configOverride != nil {
		return e.configOverride(ctx)
	}
	if e.flags == nil {
		return unresolvedConfig(e.expDefaults)
	}
	return e.flags.Experiment(ctx, e.expDefaults)
}

// unresolvedConfig is the ExperimentConfig when no flag resolves (#1907): every
// tuning knob at its compiled default and every gate closed (experiments_enabled,
// experiment_dynamic_enabled and the proposer flag half; C-034).
func unresolvedConfig(def flags.ExperimentDefaults) flags.ExperimentConfig {
	return flags.ExperimentConfig{
		Enabled:                    false,
		DynamicEnabled:             false,
		Tick:                       def.Tick,
		DynamicMaxConcurrent:       def.DynamicMaxConcurrent,
		VerdictMinN:                def.VerdictMinN,
		VerdictMinPairs:            def.VerdictMinPairs,
		MaxGames:                   def.MaxGames,
		ShadowEffectiveConcurrency: def.ShadowEffectiveConcurrency,
		VerdictEffectThreshold:     def.VerdictEffectThreshold,
		VerdictMinRawEffect:        def.VerdictMinRawEffect,
		VerdictSDFloor:             def.VerdictSDFloor,
		VerdictAnchorMargin:        def.VerdictAnchorMargin,
		CompletedGrace:             def.CompletedGrace,
		ProposerEnabled:            false,
	}
}

// tickInterval resolves the live AllocateAndSpawn cadence (experiment_tick_seconds),
// flooring a non-positive value at the compiled default (durationFlag already
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
// decision loop obeys. When enabled, it lazily runs the one-time rehydrate, the seed
// (startup-gate inversion), applies the live caps, then allocates.
func (e *experimentLoop) allocateIfEnabled(ctx context.Context) {
	// UC-043 A1 / UC-045 step 5 (#1704): with the kill switch off (or failed closed)
	// every tick reverts the autonomous promotions this agent run applied, whatever
	// experiments_enabled says, so none survives the switch.
	agentOn := e.agentEnabled(ctx)
	if !agentOn {
		e.revertPromotionsOnKillSwitch(ctx)
	}
	cfg := e.config(ctx)
	// #1907: an `off` experiment_seed re-arms the seed on ANY tick, so it is seen
	// while the gates are off too (agent-dryrun-enable.sh off clears them first).
	e.observeSeedOff(ctx, cfg)
	if !cfg.Enabled {
		if ids := e.registry.AbortAll(ctx, "experiments_enabled flag off"); len(ids) > 0 {
			e.log.Warn("experiment: experiments_enabled flag off — aborted live experiments (#1044)", "count", len(ids))
		}
		return
	}
	if !agentOn {
		if ids := e.registry.AbortAll(ctx, "kill-switch: agent disabled"); len(ids) > 0 {
			e.log.Warn("experiment: kill-switch active — aborted live experiments", "count", len(ids))
		}
		return
	}
	// Apply the live runtime caps (#1044) before the bootstrap and the allocation so
	// a hot-reloaded concurrency / games-budget / min-N takes effect on THIS tick. It
	// runs BEFORE the seed (#1907): the seed Declare reconciles
	// target_n against the registry's min-N, so the live verdict_min_n must be in
	// place first, not the compiled default the registry was built with. The games
	// budget is capped by the operator's env ceiling (#1810, C-034, FR-152 f).
	e.registry.Reconfigure(cfg.ShadowEffectiveConcurrency, e.maxGamesBudget(cfg), cfg.VerdictMinN, cfg.VerdictMinPairs)
	// #1214: mirror the four migrated FLOAT verdict knobs onto the verdict the same way,
	// so a hot-reloaded verdict_effect_threshold / verdict_min_raw_effect /
	// verdict_sd_floor / verdict_anchor_margin takes effect on THIS tick's verdict
	// evaluations. Verdict-only (the registry reads none of them), so it is a separate
	// forward to the verdict alongside Reconfigure.
	e.registry.ReconfigureVerdictFloats(cfg.VerdictEffectThreshold, cfg.VerdictMinRawEffect, cfg.VerdictSDFloor, cfg.VerdictAnchorMargin)
	// First enabled tick: run the deferred one-time rehydrate (the startup-gate
	// inversion bootstrap, #1044).
	e.ensureStarted()
	// The experiment seed (#1907): after Reconfigure (so its target_n meets the live
	// min-N) and the rehydrate (so a resumed experiment on its flag satisfies it),
	// and BEFORE the dynamic declarer, so the seed takes the budget slot first.
	e.maybeDeclareSeed(ctx, cfg)
	// Heuristic dynamic declaration (#995): both gates are on (checked above) and the
	// declarer is opted-in (live experiment_dynamic_enabled, #1044), so let the agent
	// CHOOSE a fresh experiment to declare — bounded by the LIVE concurrent-experiment
	// backstop and the per-flag dedup. This runs BEFORE AllocateAndSpawn so a
	// freshly-Started experiment can accrue games on the same tick.
	e.declareDynamicIfEnabled(ctx, cfg)
	// Retro-seeded thesis proposal (#1184): close the game→retro→thesis→experiment
	// loop. When the proposer opt-in is on (default OFF), turn the oldest recorded
	// retro suggestion into a candidate experiment whose thesis is seeded from the
	// retro's reasoning. It runs AFTER dynamic declaration (so a retro-seeded
	// experiment is the marginal one when both are enabled) and BEFORE AllocateAndSpawn
	// (so a freshly-declared experiment accrues games this tick).
	e.declareFromRetroIfEnabled(ctx, cfg)
	if n := e.registry.AllocateAndSpawn(ctx); n > 0 {
		e.log.Debug("experiment: spawned shadow games this tick", "count", n,
			"in_flight", e.registry.TotalInFlight(), "capacity", e.registry.Capacity())
	}
}

// promotionKillSwitch is the Promoter half the kill switch drives (#1704):
// RevertOnKillSwitch restores the previous real default of every autonomous
// promotion still applied, stops its re-watch and returns what it reverted.
type promotionKillSwitch interface {
	RevertOnKillSwitch(ctx context.Context) []promote.KillSwitchRevert
}

// deferredPromotions is the Promoter half that owns the deferred real writes of
// hold keys (UC-043 A17 step 3).
type deferredPromotions interface {
	DrainDeferred(ctx context.Context, resolve promote.ConfigResolver)
	DropDeferred(ctx context.Context, reason string)
	RestoreDeferredReverts(ctx context.Context, restored []experiment.RestoredRevert)
}

// pendingPromotionFlags is the registry's view of the promoter's waiting writes;
// nil without a promoter.
func pendingPromotionFlags(p *promote.Promoter) func() []string {
	if p == nil {
		return nil
	}
	return p.PendingFlags
}

// drainDeferred runs one pass of the deferred queue with the live promotion
// config resolver (read per pass and per write, opposer m2). The promoter journals
// each outcome.
func (e *experimentLoop) drainDeferred(ctx context.Context) {
	if e.deferred == nil || e.promotionConfig == nil {
		return
	}
	e.deferred.DrainDeferred(ctx, e.promotionConfig)
}

// revertPromotionsOnKillSwitch reverts the applied autonomous promotions and
// journals each revert on its experiment (outcome reverted, reason kill_switch).
// The Promoter returns a promotion's failed revert only once (#1753), so each
// returned item is journaled as it is: retries add no event.
func (e *experimentLoop) revertPromotionsOnKillSwitch(ctx context.Context) {
	if e.promotions == nil {
		return
	}
	for _, rv := range e.promotions.RevertOnKillSwitch(ctx) {
		e.log.Warn("experiment: kill-switch reverted an autonomous promotion (#1704)",
			"experiment_id", rv.ExperimentID, "flag", rv.FlagKey,
			"outcome", string(rv.Outcome), "reason", rv.Reason)
		if rv.ExperimentID == "" || e.registry == nil {
			continue
		}
		e.registry.RecordPromotion(rv.ExperimentID, experiment.PromotionResult{
			Outcome: string(rv.Outcome),
			Reason:  rv.Reason,
		})
	}
}

// journalRewatchRevert journals a re-watch revert of an autonomous promotion on
// its experiment (UC-043 step 7 and A9, #1883). The Promoter calls it from the
// re-watch goroutine at most once per promotion, with the re-watch's one
// outcome (`reverted`, or `applied` when the revert failed); a later retry is the
// kill switch's (A1). The registry is read at call time and RecordPromotion takes
// the registry lock.
func (e *experimentLoop) journalRewatchRevert(experimentID string, res experiment.PromotionResult) {
	e.log.Warn("experiment: re-watch journaled an autonomous promotion revert (#1883)",
		"experiment_id", experimentID, "outcome", res.Outcome, "reason", res.Reason)
	if e.registry == nil {
		return
	}
	e.registry.RecordPromotion(experimentID, res)
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
// experiment_dynamic_enabled flag is off (#1044), so the seed-only path is
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
		return // declarer not wired (test path): seed-only behavior unchanged.
	}
	if !cfg.DynamicEnabled {
		return // LIVE experiment_dynamic_enabled off (#1044): seed-only behavior.
	}
	// Serialize the entire select→budget-check→dedup→declare sequence so only one
	// dynamic declare is ever in flight (closes the TOCTOU over-declaration window)
	// and d.cursor is mutated under the lock (closes the data race).
	e.dynamic.mu.Lock()
	defer e.dynamic.mu.Unlock()
	// #1907: this tick's experiment_dynamic_* settings, applied under mu so a
	// concurrent Declare never sees a half-updated declarer.
	e.dynamic.configure(cfg)

	// Over-declaration backstop: stop once the LIVE experiment set fills the
	// concurrent-experiment budget (concurrentBudget: the live flag capped by the env
	// ceiling and the shadow capacity, #1810).
	if e.registry.LiveCount() >= e.concurrentBudget(cfg) {
		return
	}
	// Per-flag dedup: never stack a second experiment on a flag already under test.
	intent, ok := e.dynamic.Declare(e.registry.ActiveFlags())
	if !ok {
		return // nothing declarable this tick (no free candidate flag).
	}
	if _, err := e.declareAndStart(ctx, intent); err != nil {
		// A type-mismatch / unknown-flag rejection at Start fails closed (no shadow
		// game runs); log it and move on — the next tick rotates to another flag.
		e.log.Warn("experiment: dynamic declare/start failed (#995)",
			"flag_key", intent.FlagKey, "objective", intent.Objective, "error", err)
		return
	}
	e.log.Info("experiment: dynamic declarer declared a new experiment (#995)",
		"flag_key", intent.FlagKey, "value", intent.ExperimentalValue, "objective", intent.Objective)
}

// declareFromRetroIfEnabled lets the retro-seeded THESIS proposer (#1184) declare
// ONE fresh experiment per call when:
//   - the proposer is wired (proposer != nil) AND the proposer gate is open (the
//     AGENT_EXPERIMENT_PROPOSER_ENABLED ceiling AND the live
//     experiment_proposer_enabled flag, #1184/#1810, C-034), AND
//   - the dynamic selector is wired (the proposer DELEGATES flag/value choice to it —
//     it adds only the thesis provenance, no new selection surface), AND
//   - the live experiment count is below the SAME concurrent-experiment backstop the
//     dynamic declarer obeys (clamped to shadow capacity), AND
//   - there is a recorded retro suggestion AND the selector finds a declarable flag.
//
// It is a NO-OP while the gate is closed, so declaration is byte-identical for
// everyone who has not set AGENT_EXPERIMENT_PROPOSER_ENABLED (or has the flag off). Reached only from
// allocateIfEnabled, which already confirmed experiments_enabled + the kill-switch —
// so the proposer is subordinate to the SAME gates as every other declaration. At
// most ONE experiment is declared per call (one retro suggestion drained per tick),
// keeping the rate bounded.
//
// CONCURRENCY: it holds the dynamic declarer's mutex around the whole
// budget-check → select → declare sequence (the proposer delegates to e.dynamic.Declare,
// which mutates the shared round-robin cursor), mirroring declareDynamicIfEnabled so a
// retro declare and a dynamic declare can never race the cursor or over-declare past
// the budget. The ThesisProposer's own backlog mutex is separate and never held across
// a registry call.
func (e *experimentLoop) declareFromRetroIfEnabled(ctx context.Context, cfg flags.ExperimentConfig) {
	if e.proposer == nil || e.dynamic == nil {
		return // proposer/selector not wired (test path): declaration unchanged.
	}
	if !proposerGateOpen(cfg.ProposerEnabled) {
		return // proposer gate closed (#1184 opt-in, #1810 env AND flag): no retro-seeded experiments.
	}
	// Serialize with the dynamic declarer (shared cursor + the same over-declaration
	// budget window) — see declareDynamicIfEnabled's CONCURRENCY note.
	e.dynamic.mu.Lock()
	defer e.dynamic.mu.Unlock()
	// #1907: the proposer selects through the same declarer, so it gets the same
	// per-tick experiment_dynamic_* settings (built-in denylist included).
	e.dynamic.configure(cfg)

	if e.registry.LiveCount() >= e.concurrentBudget(cfg) {
		return
	}
	// Adapt the #995 dynamic declarer to the proposer's FlagSelector: its Declare takes
	// the main-package activeFlags defined type, so a one-line closure bridges it to the
	// proposer's map[string]struct{} selector signature.
	intent, ok := e.proposer.Propose(
		func(active map[string]struct{}) (experiment.Intent, bool) { return e.dynamic.Declare(active) },
		e.registry.ActiveFlags(),
	)
	if !ok {
		return // no recorded suggestion, or nothing declarable this tick.
	}
	if _, err := e.declareAndStart(ctx, intent); err != nil {
		e.log.Warn("experiment: retro-seeded declare/start failed (#1184)",
			"flag_key", intent.FlagKey, "objective", intent.Objective, "error", err)
		return
	}
	e.log.Info("experiment: retro-seeded thesis proposer declared a new experiment (#1184)",
		"flag_key", intent.FlagKey, "value", intent.ExperimentalValue, "objective", intent.Objective,
		"thesis", intent.Hypothesis, "backlog_remaining", e.proposer.Backlog())
}

// ensureStarted runs the deferred ONE-TIME bootstrap the FIRST time the loop
// observes experiments_enabled=true (the startup-gate inversion, #1044): rehydrate
// the durable journal (#831 resume). It is idempotent + concurrency-safe (startMu +
// startedOnce), so the many concurrent callers of allocateIfEnabled run it exactly
// once. A rehydrate error is logged and the bootstrap is STILL marked done (we do
// not want to re-rehydrate every tick). After a teardown (on→off AbortAll) a
// re-enable resumes work via the dynamic declarer (if experiment_dynamic_enabled is
// on) and a seed value not acted on yet; a seed value already acted on is never
// declared again (maybeDeclareSeed, #1051, #1907; TestExperimentsEnabled_OffOnOff).
func (e *experimentLoop) ensureStarted() {
	e.startMu.Lock()
	defer e.startMu.Unlock()
	if e.startedOnce {
		return
	}
	e.startedOnce = true
	if err := e.rehydrate(); err != nil {
		e.log.Error("experiment: rehydrate from journal failed (continuing without prior experiments)", "error", err)
	}
	// UC-043 A17 step 3: re-queue the hold-key reverts a previous run left waiting.
	if e.deferred != nil && e.registry != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		e.deferred.RestoreDeferredReverts(ctx, e.registry.TakeRestoredReverts())
		cancel()
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

// proposalSink is where the M7-4 Proposer's proposals go: declared as
// experiments in this loop's registry, under the same concurrent-experiment
// budget and declarer mutex as the dynamic declarer (PR #1417 review B2).
func (e *experimentLoop) proposalSink() *experiment.RegistrySink {
	sink := &experiment.RegistrySink{
		Registry:  e.registry,
		Objective: decision.ObjectiveBalanced,
		Budget: func(ctx context.Context) int {
			return e.concurrentBudget(e.config(ctx))
		},
	}
	if e.dynamic != nil {
		sink.Lock = &e.dynamic.mu
	}
	return sink
}

// declareAndStart declares one experiment and writes its shadow targeting,
// transitioning it to RUNNING so it can accrue shadow games. A failed start aborts
// the experiment with start_failed (UC-043 A8, #1700), as the M7-4 proposer's
// RegistrySink does, so a refused start never leaves it PROPOSED holding its flag
// under test and a slot of the concurrent-experiment budget.
func (e *experimentLoop) declareAndStart(ctx context.Context, in experiment.Intent) (string, error) {
	id, err := e.registry.Declare(in)
	if err != nil {
		return "", err
	}
	if err := e.registry.Start(ctx, id); err != nil {
		_ = e.registry.Abort(ctx, id, experiment.AbortReasonStartFailed)
		return id, err
	}
	return id, nil
}

// onGameEnd is the conclusion hook chained onto the Store's OnGameEnd (#928): when
// a shadow game that belongs to an experiment ends, fold its fitness sample into
// the matching arm via ConcludeGame. A game bound to one of the registry's
// experiments folds only once the runner has classified its terminal outcome as a
// sample (UC-046 BR-016, #1405): the telemetry end carries no outcome, so it is
// joined with onGameTerminal (joinTelemetryEnd). A game not bound to any
// experiment (no ExperimentID, or an id the registry does not own) is ignored by
// ConcludeGame, so this hook is a safe no-op for every NON-experiment game — it
// never changes the existing per-game retro/summary path. After a conclusion it
// nudges AllocateAndSpawn so the freed slot is refilled promptly (capacity churn).
func (e *experimentLoop) onGameEnd(gc gamecontext.GameContext) {
	// A stale end (UC-041 A4, #1494) means the game's telemetry stopped mid-game:
	// its snapshot is truncated, so it is never scored. An experiment game's slot is
	// released without a sample (ReleaseGame, as for an errored game, #1014) and a
	// real game never seeds the recent-real baseline.
	if gc.EndReason == gamecontext.EndReasonStale {
		reason := experiment.ReleaseReasonStale
		// A preemption the runner already reported keeps its reason (BR-015).
		if outcome, ok := e.forgetGameEnd(gc.SessionID); ok && outcome == gamerunner.OutcomePreempted {
			reason = experiment.ReleaseReasonPreempted
		}
		e.releaseStaleGame(gc, reason)
		return
	}
	// The coordinator sets the FINAL game_duration_seconds in the session teardown,
	// so for an agent-spawned shadow game it routinely loses the race against the
	// game_active=0 datapoint that fires THIS hook — leaving gc.Session.DurationSeconds
	// nil at conclusion (before #1305, when the gauge was end-only) or the last live
	// ~1 Hz value (since #1305), up to an export interval short. That nil made every
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
		// never stored — leaving the recent-real anchor with no real-game signal.
		// Record it into the finalized-per-game store (keyed by game_id) so the
		// anchor can read the mean of the last N real games. This is a pure ADD:
		// a non-experiment game still does nothing else (no cohort fold, no allocate).
		e.recordFinalizedRealFitness(gc, duration)
		return
	}

	e.joinTelemetryEnd(gc)
}

// joinTelemetryEnd is the telemetry half of the end join (UC-046 BR-016, #1405).
// With the runner's outcome already in, it folds a sample outcome and drops any
// other (that game was already released). Without one, a game bound to this
// registry is parked until onGameTerminal reports, bounded by parkWindow; an
// unbound or foreign id takes the direct path (ConcludeGame ignores it). The fold
// decision and the map change happen under graceMu; the fold runs outside it.
func (e *experimentLoop) joinTelemetryEnd(gc gamecontext.GameContext) {
	gameID := gc.SessionID
	// Read before graceMu: IsBound takes the registry lock and the window reads
	// the live config. An outcome recorded in between is still seen below.
	bound := e.registry.IsBound(gameID)
	window := e.parkWindow()

	e.graceMu.Lock()
	if outcome, ok := e.outcomes[gameID]; ok {
		delete(e.outcomes, gameID)
		e.stopTimerLocked(gameID)
		e.graceMu.Unlock()
		if isSampleOutcome(outcome) {
			e.concludeExperimentGame(gc)
		}
		return
	}
	if !bound {
		e.graceMu.Unlock()
		e.concludeExperimentGame(gc)
		return
	}
	if e.graceShutdown {
		// The loop is shutting down: neither park (no timer may outlive run())
		// nor fold a game whose outcome is unknown.
		e.graceMu.Unlock()
		return
	}
	if e.parked == nil {
		e.parked = make(map[string]gamecontext.GameContext)
	}
	e.parked[gameID] = gc
	e.armTimerLocked(gameID, window, func() { e.fireParkedEnd(gameID) })
	e.graceMu.Unlock()
}

// concludeExperimentGame scores one experiment game and folds its sample into its
// arm (the pre-#1405 onGameEnd body, unchanged): fitness span, finalized fitness,
// ConcludeGame inside experiment.evaluate, then the refill.
func (e *experimentLoop) concludeExperimentGame(gc gamecontext.GameContext) {
	duration := 0.0
	if gc.Session.DurationSeconds != nil {
		duration = *gc.Session.DurationSeconds
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
	// Record the SHADOW game's finalized fitness keyed by game_id (#965). This is the
	// same scalar ConcludeGame folds into the cohort journal.
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

// releaseStaleGame frees a stale-ended experiment game's in-flight slot without
// folding a fitness sample (#1494), under reason (ReleaseReasonStale, or
// ReleaseReasonPreempted when the runner already reported a preemption), and
// refills the freed slot. A non-experiment game, or a loop without a registry,
// needs nothing.
func (e *experimentLoop) releaseStaleGame(gc gamecontext.GameContext, reason string) {
	if e.log != nil {
		e.log.Warn("experiment: stale-ended game excluded from fitness (telemetry lost)",
			"game_id", gc.SessionID, "experiment_id", gc.ExperimentID, "arm", gc.Arm)
	}
	if gc.ExperimentID == "" || e.registry == nil {
		return
	}
	if e.registry.ReleaseGame(gc.SessionID, reason) {
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
	// #1188: re-parent under the experiment ROOT span for an experiment-bound game so
	// the per-game fitness is a CHILD of the experiment (the experiment outlives its
	// games, so this is timeline-correct). The gameLink is RETAINED — the span is still
	// navigable to the originating game trace as before. A non-experiment game / unwired
	// tracer / no root span yields an invalid SpanContext, so experimentParentCtx falls
	// back to context.Background(): the current own-root behavior, unchanged.
	ctx := e.experimentParentCtx(gc.ExperimentID)
	_, span := e.tracer.Start(ctx, decision.SpanExperimentFitness, opts...)
	defer span.End()

	fitness := e.fitness(gc, objective)
	span.SetAttributes(attribute.Float64(decision.AttrExperimentFitness, fitness))
	return fitness
}

// experimentParentCtx returns a context whose remote parent is the experiment's
// long-lived agent.experiment ROOT span (#1188), for re-parenting an experiment-bound
// game's analysis spans (experiment.fitness / evaluate) under it. It reads the root
// span's SpanContext from the registry by experiment_id and installs it as the remote
// parent via gamecontext.RemoteParentFromSpanContext.
//
// GRACEFUL FALLBACK: an empty/unknown experiment_id, an unwired registry, or a missing
// root span (the experiment torn down, or the tracer disabled) returns plain
// context.Background() — the pre-#1188 own-root behavior — so a NON-experiment game and
// the no-telemetry test path are unchanged and span emission is never broken.
func (e *experimentLoop) experimentParentCtx(experimentID string) context.Context {
	base := context.Background()
	if experimentID == "" || e.registry == nil {
		return base
	}
	sc, ok := e.registry.ExperimentSpanContext(experimentID)
	if !ok {
		return base
	}
	ctx, _ := gamecontext.RemoteParentFromSpanContext(base, sc)
	return ctx
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
	// #1188: re-parent experiment.evaluate under the experiment ROOT span (same
	// graceful-fallback as scoreGameFitness — own-root for a non-experiment game). The
	// experiment.verdict child below inherits this ctx, so it stays a child of evaluate.
	parent := e.experimentParentCtx(gc.ExperimentID)
	ctx, span := e.tracer.Start(parent, decision.SpanExperimentEvaluate, opts...)
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
// store keyed by game_id (#965): the same defaultGameFitness scalar ConcludeGame
// folds into the cohort journal, settled (post-withEffectiveDuration). A nil
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
		GameKind:  normalizedGameKind(gc),
		DurationS: duration,
	})
}

// recordFinalizedRealFitness writes a REAL primary game's finalized fitness into
// the store (#1017 §3 item 1) — the previously-DROPPED real-game fitness the
// recent-real anchor needs. A real game is not bound to one objective, so it is
// scored against EVERY experimentable objective its GameContext can evaluate and a
// per-objective sample is recorded; the anchor then filters by the experiment's
// objective. Only objectives the fitness function
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
	if kind := normalizedGameKind(gc); kind != experiment.GameKindReal {
		// An empty or unknown kind is dropped too (FR-041, fail closed). Log it so an
		// empty recent-real baseline is diagnosable (unlabeled real games).
		if kind != gamecontext.GameKindShadow && e.log != nil {
			e.log.Debug("experiment: game skipped for the real baseline: game_kind empty or unknown",
				"game_id", gc.SessionID, "game_kind", kind)
		}
		return
	}
	startedAt := gameStartedAt(gc)
	eval := decision.EvaluateFitness(gc, e.fitnessThresholds())
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
			StartedAt: startedAt,
		})
	}
}

// normalizedGameKind returns the game's kind label in the flag-evaluation
// vocabulary (gamecontext.NormalizeGameKind: telemetry "primary" -> "real",
// #1249). An empty or unknown label is passed through unchanged and is NOT real:
// only a game explicitly labeled real (or "primary") may seed the recent-real
// baseline (FR-041, fail closed). An unlabeled game may be a shadow game whose
// signals carried no game_kind, so treating it as real would let shadow fitness
// pollute the baseline the verdict anchors on.
func normalizedGameKind(gc gamecontext.GameContext) string {
	return gamecontext.NormalizeGameKind(gc.GameKind)
}

// isSampleOutcome reports whether a runner terminal outcome gives a fitness
// sample (UC-046 BR-016): a natural end (completed: winner, tie), the safety cap
// (timeout, BR-013) or the agent's own end_game (agent_ended, A14). Every other
// outcome (preempted, force_ended, error) is released without a sample.
func isSampleOutcome(outcome gamerunner.Outcome) bool {
	return outcome == gamerunner.OutcomeCompleted || outcome == gamerunner.OutcomeTimeout ||
		outcome == gamerunner.OutcomeAgentEnded
}

// onGameTerminal is the runner half of the end join (UC-046 BR-016, #1405),
// invoked by a spawned game's drive goroutine once the runner has classified the
// game's terminal outcome.
//
// When the telemetry end is already parked, a sample outcome folds it and any
// other outcome drops it. Otherwise the outcome is recorded for the telemetry end
// still to come: a sample outcome arms the #1020 completed grace (a dropped
// telemetry end must not leak the slot), and every recorded outcome is bounded by
// a timer so the map cannot grow.
//
// A non-sample outcome (error, preemption, foreign force-end, #1014) releases the
// in-flight slot without a sample, so an effective_concurrency=1 loop never waits
// on a telemetry end that may never arrive; a preemption keeps its reason so the
// pair's rerun does not use up its 3-rerun budget (BR-015).
func (e *experimentLoop) onGameTerminal(gameID string, outcome gamerunner.Outcome) {
	sample := isSampleOutcome(outcome)
	grace := e.config(context.Background()).CompletedGrace
	window := parkWindowFor(grace)

	e.graceMu.Lock()
	parked, wasParked := e.parked[gameID]
	if wasParked {
		delete(e.parked, gameID)
		e.stopTimerLocked(gameID)
	} else if !e.graceShutdown {
		if e.outcomes == nil {
			e.outcomes = make(map[string]gamerunner.Outcome)
		}
		e.outcomes[gameID] = outcome
		if !sample || !e.armCompletedGraceLocked(gameID, grace) {
			e.armTimerLocked(gameID, window, func() { e.expireOutcome(gameID) })
		}
	}
	e.graceMu.Unlock()

	if sample {
		if wasParked {
			e.concludeExperimentGame(parked)
		}
		return
	}
	reason := "game ended without usable fitness sample: " + string(outcome)
	if outcome == gamerunner.OutcomePreempted {
		// UC-046 BR-015: carry the coordinator's preempted_by_real_game so the pair's
		// rerun does not use up its 3-rerun budget.
		reason = experiment.ReleaseReasonPreempted
	}
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

// parkWindow is how long a parked telemetry end waits for the runner's outcome:
// the live experiment_completed_grace_seconds, never below parkFloor.
func (e *experimentLoop) parkWindow() time.Duration {
	return parkWindowFor(e.config(context.Background()).CompletedGrace)
}

// parkWindowFor returns max(grace, parkFloor): grace 0 (off) still bounds a park.
func parkWindowFor(grace time.Duration) time.Duration {
	if grace < parkFloor {
		return parkFloor
	}
	return grace
}

// fireParkedEnd is the park backstop (#1405): a telemetry end whose runner
// outcome never arrived within the park window is released WITHOUT a sample
// (fail closed). That covers a game this process did not drive to its end (the
// runner never reported) and keeps the parked map bounded.
func (e *experimentLoop) fireParkedEnd(gameID string) {
	e.graceMu.Lock()
	if _, ok := e.parked[gameID]; !ok {
		// The outcome arrived first and resolved the park.
		e.graceMu.Unlock()
		return
	}
	delete(e.parked, gameID)
	delete(e.graceTimers, gameID)
	if e.graceShutdown {
		e.graceMu.Unlock()
		return
	}
	e.graceMu.Unlock()

	if e.registry.ReleaseGame(gameID, releaseReasonNoOutcome) {
		e.log.Warn("experiment: telemetry end got no terminal outcome — released without a sample (#1405)",
			"game_id", gameID)
		e.allocateIfEnabled(context.Background())
	}
}

// expireOutcome drops a recorded terminal outcome whose telemetry end never
// arrived (#1405): the slot was already released (non-sample outcome) or is left
// to the completed grace, so only the map entry goes.
func (e *experimentLoop) expireOutcome(gameID string) {
	e.graceMu.Lock()
	delete(e.outcomes, gameID)
	delete(e.graceTimers, gameID)
	e.graceMu.Unlock()
}

// forgetGameEnd drops any join state of gameID (a recorded outcome, a parked end
// and its timer) and returns the recorded outcome, if any. The stale path uses it
// (UC-041 A4): a stale end resolves the game on its own.
func (e *experimentLoop) forgetGameEnd(gameID string) (gamerunner.Outcome, bool) {
	e.graceMu.Lock()
	defer e.graceMu.Unlock()
	outcome, ok := e.outcomes[gameID]
	delete(e.outcomes, gameID)
	delete(e.parked, gameID)
	e.stopTimerLocked(gameID)
	return outcome, ok
}

// armCompletedGrace schedules the #1020 bounded-grace backstop for a COMPLETED
// game with the live grace window and reports whether it armed one (grace 0, off,
// arms nothing).
func (e *experimentLoop) armCompletedGrace(gameID string) bool {
	// #1810: the window is read at use time (experiment_completed_grace_seconds, else
	// the env fallback, else 60 s), so a flag write reaches the next completed game.
	grace := e.config(context.Background()).CompletedGrace
	e.graceMu.Lock()
	defer e.graceMu.Unlock()
	return e.armCompletedGraceLocked(gameID, grace)
}

// armCompletedGraceLocked arms the #1020 grace for gameID: after grace, if the
// game is still in flight (its telemetry end never arrived), release its slot
// non-counting (no fabricated fitness sample) so the effective_concurrency=1 loop
// cannot deadlock on a dropped game_active=0 datapoint. Returns false for grace
// 0 (off: the pre-#1020 trust-telemetry behaviour). Caller holds graceMu.
func (e *experimentLoop) armCompletedGraceLocked(gameID string, grace time.Duration) bool {
	if grace <= 0 {
		return false
	}
	e.armTimerLocked(gameID, grace, func() { e.fireCompletedGrace(gameID) })
	return true
}

// armTimerLocked schedules f after d as gameID's single join timer, replacing
// (and stopping) any prior one so only one timer fires per id. Timers are tracked
// so run() can stop them on shutdown. Caller holds graceMu.
func (e *experimentLoop) armTimerLocked(gameID string, d time.Duration, f func()) {
	after := e.afterFunc
	if after == nil {
		after = func(d time.Duration, f func()) func() bool {
			t := time.AfterFunc(d, f)
			return t.Stop
		}
	}
	if e.graceTimers == nil {
		e.graceTimers = make(map[string]func() bool)
	}
	e.stopTimerLocked(gameID)
	e.graceTimers[gameID] = after(d, f)
}

// stopTimerLocked cancels and forgets gameID's join timer, if any. Caller holds
// graceMu.
func (e *experimentLoop) stopTimerLocked(gameID string) {
	if stop, ok := e.graceTimers[gameID]; ok {
		stop()
		delete(e.graceTimers, gameID)
	}
}

// fireCompletedGrace is the grace-timer callback: it forgets the timer and the
// recorded outcome and, if the game is still in-flight (its telemetry end never
// arrived — a dropped datapoint), releases its slot non-counting and refills. If
// the telemetry end already folded the game, ReleaseGame finds nothing and no-ops.
// A fired backstop is logged distinctly so a real telemetry drop is OBSERVABLE
// rather than silent.
func (e *experimentLoop) fireCompletedGrace(gameID string) {
	e.graceMu.Lock()
	delete(e.graceTimers, gameID)
	delete(e.outcomes, gameID)
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
			"game_id", gameID)
		// Refill the freed slot (respecting the kill-switch). context.Background is
		// intentional here for the same reason as the #1014 release path: this runs on
		// a runtime timer goroutine with no request/tick context to thread.
		e.allocateIfEnabled(context.Background())
	}
}

// stopAllCompletedGrace cancels every pending join timer and drops the join state
// (shutdown teardown), so no timer fires and nothing is folded after the loop
// returns. Called from run() on ctx cancellation alongside AbortAll.
func (e *experimentLoop) stopAllCompletedGrace() {
	e.graceMu.Lock()
	// Latch shutdown FIRST: any callback already firing (whose Stop() returns false)
	// will observe this under graceMu and bail without spawning, and a telemetry end
	// arriving later is neither parked nor folded (joinTelemetryEnd).
	e.graceShutdown = true
	for id, stop := range e.graceTimers {
		stop()
		delete(e.graceTimers, id)
	}
	clear(e.parked)
	clear(e.outcomes)
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
//
// It scores with the flagd-schema default thresholds; the production loop scores
// with the live fitness.* flags through gameFitnessWith (#1740).
func defaultGameFitness(gc gamecontext.GameContext, objective string) float64 {
	return scoreGameFitness(gc, objective, decision.DefaultStaticConfig().FitnessThresholds)
}

// gameFitnessWith returns the gameFitnessFunc that scores against src's fitness.*
// thresholds, read at each game end so an operator's threshold change applies to
// the next game (UC-043 step 4, D-F5, #1740). A nil src scores with the defaults.
func gameFitnessWith(src decision.FitnessSource) gameFitnessFunc {
	if src == nil {
		return defaultGameFitness
	}
	return func(gc gamecontext.GameContext, objective string) float64 {
		return scoreGameFitness(gc, objective, src.Fitness())
	}
}

// scoreGameFitness is the shared body of defaultGameFitness / gameFitnessWith.
func scoreGameFitness(gc gamecontext.GameContext, objective string, thresholds decision.FitnessThresholds) float64 {
	if objective == "" {
		objective = decision.ObjectiveBalanced
	}
	eval := decision.EvaluateFitness(gc, thresholds)
	if r, ok := eval.Results[objective]; ok && r.Evaluated {
		return r.Progress
	}
	return 0
}

// fitnessThresholds returns the fitness.* thresholds in force now: the live source
// when wired (#1740), else the flagd-schema defaults.
func (e *experimentLoop) fitnessThresholds() decision.FitnessThresholds {
	if e.thresholds == nil {
		return decision.DefaultStaticConfig().FitnessThresholds
	}
	return e.thresholds.Fitness()
}

// flagsFitnessSource is the production decision.FitnessSource of the experiment
// loop: it reads the agent fitness.* flags at each call (never cached, #1740), the
// same flags the rules engine scores against. A nil client serves the defaults.
type flagsFitnessSource struct {
	flags *flags.Flags
}

// Fitness implements decision.FitnessSource.
func (s flagsFitnessSource) Fitness() decision.FitnessThresholds {
	if s.flags == nil {
		return decision.DefaultStaticConfig().FitnessThresholds
	}
	return decision.FitnessThresholdsFromFlags(s.flags.Evaluate(context.Background()).Fitness)
}

// gameStartedAt is when the game started, for the autonomous watch window (#1702):
// the session's StartedAt (set at the game_active 0->1 transition, never evicted),
// else the timeline's "start" phase event. The timeline is a 64-entry ring, so on a
// normal-length game the start event is long evicted by game end and only serves
// as a fallback for a context without a session start.
func gameStartedAt(gc gamecontext.GameContext) time.Time {
	if !gc.Session.StartedAt.IsZero() {
		return gc.Session.StartedAt
	}
	return timelinePhaseAt(gc.Timeline, "start")
}

// timelinePhaseAt returns the time of the session's last phase event with the
// given detail ("start", as timelinePhaseDuration reads it), or the zero time when
// there is none.
func timelinePhaseAt(timeline []gamecontext.TimelineEvent, detail string) time.Time {
	var at time.Time
	for _, ev := range timeline {
		if ev.Kind == gamecontext.EventPhase && ev.Detail == detail {
			at = ev.At
		}
	}
	return at
}

// withEffectiveDuration returns gc with Session.DurationSeconds populated from the
// partition's start→end phase timeline when the coordinator's final
// game_duration_seconds value has not yet been observed: either no gauge at all
// (DurationSeconds == nil) or only a live in-game value (#1305) that is shorter
// than the start→end bracket because the final set lost the race. The larger of the
// two wins, so a present final gauge stays authoritative. It is a no-op when the
// timeline lacks a start/end pair to bracket (no signal to recover — a genuinely
// signal-less game still folds the worst-case 0, the #994 contract).
//
// This is the honest #997 fix: the recovered value is the real wall-clock game
// duration the agent's own Store stamped on the session phase events
// (SetGameActive start/end), NOT a fabricated number — so duration-based
// objectives (endurance/accelerate) get the signal they need at conclusion.
func withEffectiveDuration(gc gamecontext.GameContext) gamecontext.GameContext {
	dur, ok := timelinePhaseDuration(gc.Timeline)
	if !ok {
		return gc
	}
	if gc.Session.DurationSeconds != nil && *gc.Session.DurationSeconds >= dur {
		return gc
	}
	gc.Session.DurationSeconds = &dur
	return gc
}

// timelinePhaseDuration derives the elapsed game time from a partition's rolling
// narrative: the seconds between the session's "start" phase event and its "end"
// phase event. It returns false when either bracket is missing or the span is
// non-positive (nothing trustworthy to recover). The end snapshot the Store hands
// OnGameEnd is taken right AFTER appending the "end" phase (store.go endGameLocked),
// so both brackets are present on a concluded game. The "end" event is stamped at
// the game's actual end (the 0's arrival, the root span's end time, or the last
// game_active datapoint), not at when the agent learned of it (#1494).
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

// experimentDefaults returns the COMPILED defaults for the migrated experiment
// runtime knobs (#1044): the per-tick fallback each live agent.json flag resolves to
// when it is absent or flagd cannot answer. Since #1907 (FR-151 Obsolete) none is
// env-derived: each equals its flag default in agent.json (pinned by
// TestCompiledDefaultsMatchFlagDefaults), and the two gates have no default here
// because flags.Experiment falls back to a literal false for them.
func experimentDefaults() flags.ExperimentDefaults {
	return flags.ExperimentDefaults{
		Tick: defaultExperimentTick,
		// #1810: the two widening settings fall back to their COMPILED defaults; their
		// env vars are ceilings (experimentCeilingsFromEnv), applied after the read.
		DynamicMaxConcurrent:       defaultDynamicMaxConcurrent,
		VerdictMinN:                experiment.DefaultMinNPerArm,
		VerdictMinPairs:            experiment.DefaultMinPairs,
		MaxGames:                   experiment.DefaultMaxGamesPerExperiment,
		ShadowEffectiveConcurrency: experiment.DefaultEffectiveConcurrency,
		CompletedGrace:             defaultCompletedGrace,
		// Verdict-tuning float knobs (#1214): the verdict's own Default* constants.
		VerdictEffectThreshold: experiment.DefaultEffectThreshold,
		VerdictMinRawEffect:    experiment.DefaultMinRawEffect,
		VerdictSDFloor:         experiment.DefaultSDFloor,
		VerdictAnchorMargin:    experiment.DefaultAnchorMargin,
	}
}

// experimentCeilings are the operator's env ceilings on the two widening
// experiment settings (#1810, C-034, FR-152 e/f). 0 = no ceiling.
type experimentCeilings struct {
	// MaxConcurrent caps experiment_dynamic_max_concurrent
	// (AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT).
	MaxConcurrent int
	// MaxGames caps experiment_max_games (AGENT_EXPERIMENT_MAX_GAMES).
	MaxGames int
}

// experimentCeilingsFromEnv reads the two ceilings once at startup. Unset = no
// ceiling. An invalid or non-positive value is no ceiling too (WARN, except a
// max-games 0, which has always meant "no games limit").
func experimentCeilingsFromEnv(logger *slog.Logger) experimentCeilings {
	if logger == nil {
		logger = slog.Default()
	}
	return experimentCeilings{
		MaxConcurrent: ceilingEnv(logger, envDynamicMaxConcurrent, true),
		MaxGames:      ceilingEnv(logger, envMaxGames, false),
	}
}

// ceilingEnv parses one ceiling env var: a positive int, else 0 (no ceiling).
func ceilingEnv(logger *slog.Logger, key string, warnZero bool) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return 0
	}
	n, err := strconv.Atoi(v)
	if err != nil || n < 0 || (n == 0 && warnZero) {
		logger.Warn("experiment ceiling env invalid or not positive; no ceiling, the flag governs (#1810, C-034)", "env", key, "value", v)
		return 0
	}
	return n
}

// concurrentBudget is the effective concurrent-experiment budget (#1810, FR-152 e)
// shared by the dynamic declarer, the thesis proposer and the M7-4 proposer sink:
// the live experiment_dynamic_max_concurrent (a non-positive value falls back to
// the compiled default 3), capped by the AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT
// ceiling when set, then clamped to the shadow capacity (an experiment with no
// shadow slot to ever run is pointless).
func (e *experimentLoop) concurrentBudget(cfg flags.ExperimentConfig) int {
	budget := cfg.DynamicMaxConcurrent
	if budget <= 0 {
		budget = e.expDefaults.DynamicMaxConcurrent
	}
	if budget <= 0 {
		budget = defaultDynamicMaxConcurrent
	}
	if ceiling := e.ceilings.MaxConcurrent; ceiling > 0 && budget > ceiling {
		budget = ceiling
	}
	if capacity := e.registry.Capacity(); capacity > 0 && budget > capacity {
		budget = capacity
	}
	return budget
}

// maxGamesBudget is the effective per-experiment games budget (#1810, FR-152 f):
// min(AGENT_EXPERIMENT_MAX_GAMES, experiment_max_games), where 0 or less means no
// limit on either side. The result is what Registry.Reconfigure takes: 0 = no limit.
func (e *experimentLoop) maxGamesBudget(cfg flags.ExperimentConfig) int {
	games := cfg.MaxGames
	if games < 0 {
		games = 0
	}
	ceiling := e.ceilings.MaxGames
	if ceiling <= 0 {
		return games
	}
	if games == 0 || games > ceiling {
		return ceiling
	}
	return games
}

// anchorBaselineRecentN is how many recent real games the #992 recent-real anchor
// averages. It mirrors the verdict's DefaultMinNPerArm (8) so the anchor rests on
// a comparable sample to what the cohort verdict already trusts (#1017 §2).
const anchorBaselineRecentN = 8

// anchorBaselineMinSamples is the minimum real samples before the anchor is
// trusted; below it the anchor is skipped (fails open). A floor of 3 means the
// anchor is never a single-game fluke.
const anchorBaselineMinSamples = 3

// newFitnessStore builds the finalized-fitness ring at its fixed compiled size
// (the AGENT_FITNESS_STORE_CAPACITY env override is gone, #1907).
func newFitnessStore() *experiment.FitnessStore {
	return experiment.NewFitnessStore(experiment.DefaultFitnessStoreCapacity)
}

// recentRealBaselineAccessor adapts the #965 FitnessStore into the verdict's #992
// RecentRealBaseline seam: given an objective it returns the mean finalized fitness
// of the last recentN REAL games and whether a TRUSTWORTHY baseline exists (>=
// minSamples real samples). It returns ok=false (anchor SKIPPED — fail open) when
// the store is unwired or too few real samples exist. store may be nil ⇒ the accessor always reports ok=false (anchor disabled).
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
