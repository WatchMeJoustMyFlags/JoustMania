package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamerunner"
)

// shadowSensitivityKey is the game flag an experiment arm's shadow start config
// takes its sensitivity from (UC-046 BR-019, #2392). The coordinator does not read
// it; only the menu (real games) and this spawner (experiment arms) do.
const shadowSensitivityKey = "sensitivity"

// armIntResolver resolves an integer game flag with an experiment arm's evaluation
// context (flags.Flags.ShadowGameInt).
type armIntResolver func(ctx context.Context, key, mode, experimentID, arm string) (int64, error)

// errSpawnNeedsArchetype: every shadow experiment game is a pattern game (UC-046).
var errSpawnNeedsArchetype = errors.New("no motion archetype on the spawn (UC-046: experiments are archetype-only)")

// shadow_spawner.go is the REAL ShadowSpawner seam (#976) the experiment registry
// (#978) injects — the production replacement for the registry_test.go fakeSpawner.
//
// SHADOW-SPAWNER DECISION (#991 investigation): the agent ALREADY owns an outbound
// game-coordinator gRPC client — the #778 gamerunner, which starts a headless
// mock-only game via StreamGameEvents+StartGameConfig and drives it to its win
// condition. #976 added experiment_id / arm / origin=AGENT to StartGameConfig
// (proto/game_coordinator.proto), and the agent's gen stubs are regenerated here so
// the Go client carries them. So this spawner is a thin wrapper over the existing
// gamerunner that BINDS (experiment_id, arm) AT SPAWN (design §5) and tags the
// start origin GAME_ORIGIN_AGENT — the receiving side (#976) classifies an
// AGENT/UNSPECIFIED origin as a shadow session and applies experiment_id/arm only
// to a shadow game (the hard real-protection invariant, epic #982 decision 7).
//
// Spawn() must return the coordinator-assigned game_id PROMPTLY (it is called on the
// registry's allocate tick under capacity book-keeping), but a gamerunner run is
// synchronous (it blocks until the game reaches a terminal event). So Spawn starts
// the run in a BACKGROUND goroutine bounded by the spawner's root context, waits
// only for the game_id to be assigned (the first stamped event), and returns it —
// the game then plays out and ends on its own. The CONCLUSION (per-arm fitness) is
// NOT returned here: it flows through the SAME telemetry path every game uses — the
// game-end span/metric carries experiment_id/arm (#975), the agent ingests it onto
// the GameContext, and the OnGameEnd hook (experimentLoop.onGameEnd) folds the
// fitness sample into the journal via Registry.ConcludeGame. Spawn owns STARTING a
// game; the observe path owns CONCLUDING it — the clean split the design intends.
type shadowSpawner struct {
	cfg  gamerunner.Config
	log  *slog.Logger
	spec gamerunner.Spec // template spec (mode/players/sensitivity); RunID/experiment per call

	// root bounds every background game-drive goroutine to the agent's lifetime, so
	// shutdown (ctx cancel) stops in-flight shadow games and never leaks a goroutine
	// (mirrors the #917 SetRootContext pattern). Set via SetRootContext before run.
	root context.Context

	// onTerminal, when set, is invoked the moment a spawned game reaches a terminal
	// outcome (#1014). The experiment loop wires it to the registry so a game that
	// ended WITHOUT a usable fitness sample (game_error / force-end) has its
	// in-flight slot released directly, independent of the telemetry path — the
	// deadlock backstop for effective_concurrency=1.
	onTerminal gamerunner.TerminalCallback

	// onPatternStart / onPatternEnd report pattern games to the registry (UC-046).
	onPatternStart gamerunner.PatternStartCallback
	onPatternEnd   gamerunner.PatternCallback

	// armInt resolves the arm's start-config sensitivity (UC-046 BR-019, #2392);
	// nil keeps the template's SHADOW_GAME_SENSITIVITY.
	armInt armIntResolver
}

// SetPatternCallbacks registers the UC-046 start and replay-record hooks.
func (s *shadowSpawner) SetPatternCallbacks(start gamerunner.PatternStartCallback, end gamerunner.PatternCallback) {
	s.onPatternStart = start
	s.onPatternEnd = end
}

// SetTerminalCallback registers the #1014 terminal-outcome hook, propagated to
// every per-spawn gamerunner so the registry frees the slot for a non-concluding
// game even if the telemetry game_active=0 path never fires for it.
func (s *shadowSpawner) SetTerminalCallback(cb gamerunner.TerminalCallback) { s.onTerminal = cb }

// newShadowSpawner builds the spawner over the gamerunner config + a template spec
// drawn from the SHADOW_GAME_* env (the same spec the #778 one-shot runner uses), so
// an operator tunes the shadow game mode/players/sensitivity with the existing knobs.
// armInt resolves an experiment arm's own sensitivity (UC-046 BR-019, #2392).
func newShadowSpawner(cfg gamerunner.Config, logger *slog.Logger, armInt armIntResolver) *shadowSpawner {
	return &shadowSpawner{
		cfg:    cfg,
		log:    logger,
		armInt: armInt,
		spec:   gamerunner.SpecFromEnv(""), // RunID filled per spawn
		root:   context.Background(),       // replaced by SetRootContext in run()
	}
}

// SetRootContext bounds every background game-drive goroutine to ctx (the agent's
// lifetime). run() calls this with the server context so shutdown cancels in-flight
// shadow games — the goroutine-leak-safe contract (#923).
func (s *shadowSpawner) SetRootContext(ctx context.Context) { s.root = ctx }

// Spawn starts ONE shadow game bound to (experimentID, arm), origin AGENT, and
// returns its coordinator-assigned game_id. It blocks only until the game_id is
// known; the game then plays to completion in a background goroutine. It satisfies
// experiment.ShadowSpawner.
//
// The CALL ctx bounds only the start handshake (the allocate tick's context); the
// game DRIVE is bounded by the spawner's ROOT context so the game is not killed
// when the tick returns — only at agent shutdown. An error means no game started
// and the registry releases the reserved capacity slot.
func (s *shadowSpawner) Spawn(ctx context.Context, experimentID, arm string, seed uint64) (string, error) {
	spec, err := s.armSpec(ctx, experimentID, arm, seed)
	if err != nil {
		return "", err
	}
	runID := spec.RunID

	runner := gamerunner.New(s.cfg, s.log)
	if s.onTerminal != nil {
		runner.SetTerminalCallback(s.onTerminal)
	}
	if s.onPatternStart != nil {
		runner.SetPatternStartCallback(s.onPatternStart)
	}
	if s.onPatternEnd != nil {
		runner.SetPatternCallback(s.onPatternEnd)
	}

	gameID, err := runner.StartExperimentGame(ctx, s.root, spec)
	if err != nil {
		// The coordinator's single-game cap rejects concurrent starts with
		// "Game already in progress" (gamerunner.ErrGameInProgress). That is
		// backpressure, not a failure: the game-coordinator runs ONE game at a
		// time and the registry (#998) should release the reservation and retry
		// on the next tick rather than WARN per spawn. Translate the gamerunner
		// sentinel into the experiment-package one the registry matches, keeping
		// the underlying coordinator message for the debug log.
		if errors.Is(err, gamerunner.ErrGameInProgress) {
			return "", fmt.Errorf("shadow spawn for experiment %s arm %s: %w: %w",
				experimentID, arm, experiment.ErrSpawnBackpressure, err)
		}
		return "", fmt.Errorf("shadow spawn for experiment %s arm %s: %w", experimentID, arm, err)
	}
	s.log.Debug("experiment: shadow game spawned",
		"experiment_id", experimentID, "arm", arm, "seed", seed, "game_id", gameID, "run_id", runID)
	return gameID, nil
}

// armSpec is the start spec of one shadow game of (experimentID, arm): the template
// spec with the run id, the experiment binding, the paired seed, the trace parent
// and the arm's pinned archetype. For an experiment arm the start-config sensitivity
// is the arm's own value of the game flag `sensitivity`, resolved with the arm's
// evaluation context (UC-046 BR-019, UC-043 BR-009, #2392), so an experiment on it
// starts its arms with different sensitivities; SHADOW_GAME_SENSITIVITY stays the
// value of a shadow game without an experiment. An unreadable or out-of-range value
// refuses the spawn: the registry rolls the pair back and retries next tick, so no
// arm starts on a value its sibling may not share.
func (s *shadowSpawner) armSpec(ctx context.Context, experimentID, arm string, seed uint64) (gamerunner.Spec, error) {
	spec := s.spec
	spec.RunID = uuid.NewString()
	spec.ExperimentID = experimentID
	spec.Arm = arm
	// Paired CRN seed (#1003): thread the registry-derived per-pair seed onto the
	// spawn spec so the game-coordinator builds the shadow session's per-instance
	// RNG from it. Both arms of a pair receive the same seed; 0 means entropy.
	spec.Seed = seed
	// Spawn-traceparent propagation (#1182, epic #1181): the registry carries the
	// experiment's long-lived ROOT span context on the spawn ctx as a remote
	// parent. Thread it onto the spec so StartExperimentGame injects it as the
	// outgoing traceparent; under telemetry.experiment_game_parent=child the
	// coordinator's game span becomes a CHILD of the experiment span, under the
	// default link it starts its own trace with a Link back (#2223). An invalid SpanContext (no root span) threads nothing, so
	// the game stays own-rooted — graceful.
	spec.ExperimentSpanContext = trace.SpanContextFromContext(ctx)

	// UC-046 step 3: the arm carries its pinned archetype, cap and flag digest on
	// the ctx. Shadow experiment games are archetype-only (maintainer decision on
	// #1258): a spawn without an archetype is refused, never run on random motion.
	p, ok := experiment.PatternSpawnFrom(ctx)
	if !ok {
		return spec, fmt.Errorf("shadow spawn for experiment %s arm %s: %w", experimentID, arm, errSpawnNeedsArchetype)
	}
	spec.Pattern = p.Archetype
	spec.MaxGameSeconds = p.MaxGameSeconds
	spec.PairIndex = p.PairIndex
	spec.FlagDigest = p.FlagDigest

	if experimentID != "" && s.armInt != nil {
		level, err := s.armInt(ctx, shadowSensitivityKey, spec.GameName, experimentID, arm)
		if err != nil {
			return spec, fmt.Errorf("shadow spawn for experiment %s arm %s: resolve %s: %w",
				experimentID, arm, shadowSensitivityKey, err)
		}
		if level < 0 || level > 4 {
			return spec, fmt.Errorf("shadow spawn for experiment %s arm %s: %s=%d outside 0..4",
				experimentID, arm, shadowSensitivityKey, level)
		}
		spec.Sensitivity = int(level)
	}
	return spec, nil
}
