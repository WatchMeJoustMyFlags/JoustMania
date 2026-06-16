package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/google/uuid"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/gamerunner"
)

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
}

// SetTerminalCallback registers the #1014 terminal-outcome hook, propagated to
// every per-spawn gamerunner so the registry frees the slot for a non-concluding
// game even if the telemetry game_active=0 path never fires for it.
func (s *shadowSpawner) SetTerminalCallback(cb gamerunner.TerminalCallback) { s.onTerminal = cb }

// newShadowSpawner builds the spawner over the gamerunner config + a template spec
// drawn from the SHADOW_GAME_* env (the same spec the #778 one-shot runner uses), so
// an operator tunes the shadow game mode/players/sensitivity with the existing knobs.
func newShadowSpawner(cfg gamerunner.Config, logger *slog.Logger) *shadowSpawner {
	return &shadowSpawner{
		cfg:  cfg,
		log:  logger,
		spec: gamerunner.SpecFromEnv(""), // RunID filled per spawn
		root: context.Background(),       // replaced by SetRootContext in run()
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
	runID := uuid.NewString()
	spec := s.spec
	spec.RunID = runID
	spec.ExperimentID = experimentID
	spec.Arm = arm
	// Paired CRN seed (#1003): thread the registry-derived per-pair seed onto the
	// spawn spec so the game-coordinator builds the shadow session's per-instance
	// RNG from it. Both arms of a pair receive the same seed; 0 means entropy.
	spec.Seed = seed

	runner := gamerunner.New(s.cfg, s.log)
	if s.onTerminal != nil {
		runner.SetTerminalCallback(s.onTerminal)
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
