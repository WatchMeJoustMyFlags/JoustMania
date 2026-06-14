// Package gamerunner drives mock-only "shadow" games from the Go agent (#778,
// phase 4 of the shadow-games initiative #774).
//
// A shadow game runs on the live stack — same controller-manager, same
// game-coordinator — using only RESERVED mock controllers, so it never appears
// in the menu lobby and never collides with a real, menu-driven game. The
// Runner:
//
//  1. AddControllers(reserved=true, tag="agent:<runID>") to mint synthetic
//     players that the menu cannot see (#777),
//  2. opens StreamGameEvents with a StartGameConfig to start the game headlessly
//     (no lobby) and learns the coordinator-assigned game_id from the first
//     stamped event (#776),
//  3. drives the game to its win condition via the mock control RPCs
//     (SimulateMovement for liveliness, SimulateDeath per player),
//  4. awaits a terminal event (or force-ends on timeout),
//  5. ALWAYS removes its reserved controllers on the way out.
//
// Unlike the intervention API (#730), which acts through flagd file writes,
// game-start has no flag representation: it is a direct gRPC capability. This is
// the agent's first set of gRPC clients toward the game stack.
package gamerunner

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	mockpb "github.com/joustmania/agent/gen/controller_manager_mock"
	gcpb "github.com/joustmania/agent/gen/game_coordinator"
)

// tracerName names the OTel tracer for shadow-game spans.
const tracerName = "github.com/joustmania/agent/gamerunner"

// Terminal game event types. Reaching any of these ends the run; they mirror the
// coordinator's published lifecycle events (game_session.py / servicer.py).
const (
	eventGameEnded      = "game_ended"
	eventGameForceEnded = "game_force_ended"
	eventGameError      = "game_error"
)

// Early game event types surfaced on the stream before the terminal phase. A
// game_start_error means the coordinator rejected the start outright.
const (
	eventGameStartError = "game_start_error"
)

// terminalEvents is the set whose arrival ends RunShadowGame's await loop.
var terminalEvents = map[string]struct{}{
	eventGameEnded:      {},
	eventGameForceEnded: {},
	eventGameError:      {},
}

// Outcome is the high-level result classification of a shadow-game run.
type Outcome string

const (
	// OutcomeCompleted means the game reached a natural terminal event
	// (game_ended) within the timeout.
	OutcomeCompleted Outcome = "completed"
	// OutcomeForceEnded means the await timed out and the runner force-ended
	// the game (and observed its terminal event).
	OutcomeForceEnded Outcome = "force_ended"
	// OutcomeError means the game emitted game_error, or the run failed before
	// reaching a terminal event.
	OutcomeError Outcome = "error"
)

// Config holds the endpoints and tuning for a Runner. Endpoints mirror the
// docker-compose service names; the zero value is filled by ConfigFromEnv.
type Config struct {
	// CoordinatorAddr is the GameCoordinatorService gRPC endpoint
	// (docker-compose: game-coordinator:50053).
	CoordinatorAddr string
	// MockAddr is the MockControllerService gRPC endpoint. The mock control API
	// rides on the controller-manager container, port 50062.
	MockAddr string

	// GameTimeout bounds how long RunShadowGame waits for a terminal event
	// before force-ending the game.
	GameTimeout time.Duration
	// MovementInterval is how often SimulateMovement is sent per controller for
	// liveliness while driving the game.
	MovementInterval time.Duration
	// KillInterval paces SimulateDeath calls. It MUST exceed the mock's
	// death-hold (~1s) so a held death does not overlap the next kill and so
	// the post-death grace window has elapsed before the next player is killed.
	KillInterval time.Duration
	// RPCTimeout bounds individual unary RPCs (AddControllers, SimulateDeath,
	// ForceEndGame, …).
	RPCTimeout time.Duration

	// ReadyTimeout bounds how long reserveControllers waits for its reserved
	// controllers to become VISIBLE (registered) to the mock service before
	// starting the game (#1013). A start that proceeds before all spec.Players
	// controllers are present can admit an under-populated game that FFA aborts
	// ("Need at least 2 players, got 1").
	ReadyTimeout time.Duration
	// ReadyPollInterval is how often the readiness gate re-checks the controller
	// roster while waiting for all reserved serials to appear.
	ReadyPollInterval time.Duration
}

// Default config values. Conservative and CI-friendly.
const (
	DefaultCoordinatorAddr  = "game-coordinator:50053"
	DefaultMockAddr         = "controller-manager:50062"
	DefaultGameTimeout      = 90 * time.Second
	DefaultMovementInterval = 250 * time.Millisecond
	DefaultKillInterval     = 1500 * time.Millisecond
	DefaultRPCTimeout       = 10 * time.Second
	// DefaultReadyTimeout bounds the readiness gate (#1013). Controller
	// registration is normally immediate, so this only ever absorbs a brief
	// propagation lag; a generous ceiling keeps it from ever masking a genuine
	// failure to reserve.
	DefaultReadyTimeout = 10 * time.Second
	// DefaultReadyPollInterval is the readiness-gate re-check cadence.
	DefaultReadyPollInterval = 100 * time.Millisecond
)

// withDefaults returns a copy of c with any unset field filled from the
// package defaults. Keeps Runner construction tolerant of partial configs.
func (c Config) withDefaults() Config {
	if c.CoordinatorAddr == "" {
		c.CoordinatorAddr = DefaultCoordinatorAddr
	}
	if c.MockAddr == "" {
		c.MockAddr = DefaultMockAddr
	}
	if c.GameTimeout <= 0 {
		c.GameTimeout = DefaultGameTimeout
	}
	if c.MovementInterval <= 0 {
		c.MovementInterval = DefaultMovementInterval
	}
	if c.KillInterval <= 0 {
		c.KillInterval = DefaultKillInterval
	}
	if c.RPCTimeout <= 0 {
		c.RPCTimeout = DefaultRPCTimeout
	}
	if c.ReadyTimeout <= 0 {
		c.ReadyTimeout = DefaultReadyTimeout
	}
	if c.ReadyPollInterval <= 0 {
		c.ReadyPollInterval = DefaultReadyPollInterval
	}
	return c
}

// Spec describes one shadow game to run.
type Spec struct {
	// RunID is a short, unique identifier for this run. It is embedded in the
	// controller reservation tag ("agent:<RunID>") so a crashed run's
	// controllers can be swept later. Required.
	RunID string
	// GameName is the coordinator game mode, e.g. "JoustFFA", "Swapper".
	GameName string
	// Players is how many mock controllers to reserve and field. Must be >= 2
	// for any mode that ends on a last-player-standing condition.
	Players int
	// Sensitivity is the common death sensitivity 0-4 (2 = MEDIUM).
	Sensitivity int

	// ExperimentID / Arm bind this shadow game to an experiment cohort AT SPAWN
	// (#976, design §5). When set, they are threaded onto StartGameConfig so the
	// started session's eval-context (targeting #977) and telemetry (attribution
	// #975) carry them from the start. Empty (the default) means an unbound shadow
	// game — the #778 one-shot runner sets neither, so its behavior is unchanged.
	// The HARD INVARIANT (epic #982 decision 7) is enforced coordinator-side: these
	// only ever subdivide a shadow game (origin AGENT), never make a real game part
	// of an experiment.
	ExperimentID string
	Arm          string

	// Seed is the deterministic RNG seed for paired CRN shadow games (#1003).
	// When set (non-zero), it is threaded onto StartGameConfig.rng_seed so the
	// game-coordinator constructs the game's per-instance RNG from it — the two
	// games in a (experimental, control) pair share a Seed so they face an
	// identical bracket order / role assignment / tempo schedule. 0 (the default)
	// means entropy: an unbound shadow game or the #778 one-shot runner sets
	// nothing, byte-unchanged from before. Honored coordinator-side ONLY for a
	// shadow session (the hard real-protection invariant, epic #982 decision 7).
	Seed uint64
}

// Tag returns the reservation tag for this run's controllers.
func (s Spec) Tag() string { return "agent:" + s.RunID }

// Result is the structured outcome of a RunShadowGame call.
type Result struct {
	// GameID is the coordinator-assigned id, captured from the event stream.
	// May be empty if the run failed before the first stamped event.
	GameID string
	// GameName echoes the spec's mode.
	GameName string
	// Serials are the reserved controllers the run created (and removed).
	Serials []string
	// Outcome classifies how the run ended.
	Outcome Outcome
	// TerminalEvent is the event_type that ended the await loop (e.g.
	// "game_ended"), or empty if none was observed.
	TerminalEvent string
	// EventsSeen is the total number of game events observed on the stream.
	EventsSeen int
	// Duration is wall-clock time from start to terminal/cleanup.
	Duration time.Duration
}

// dialer abstracts grpc.NewClient so tests can inject in-process connections.
type dialer func(target string) (grpc.ClientConnInterface, func() error, error)

// defaultDialer dials a plaintext gRPC endpoint (the Python services serve
// insecure plain gRPC inside the compose network).
func defaultDialer(target string) (grpc.ClientConnInterface, func() error, error) {
	conn, err := grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, nil, err
	}
	return conn, conn.Close, nil
}

// TerminalCallback is invoked by an experiment game's background goroutine the
// instant the game reaches a terminal outcome (#1014), carrying the
// coordinator game_id and the classified Outcome. It is the SYNCHRONOUS,
// telemetry-independent signal the experiment registry uses to release the
// in-flight slot for a game that did NOT conclude with a usable fitness sample
// (game_error / force-end / abort), so the effective_concurrency=1 loop can never
// deadlock waiting on a metric that may never arrive for a failed game. It is
// only ever called for an EXPERIMENT-bound game (StartExperimentGame).
type TerminalCallback func(gameID string, outcome Outcome)

// Runner drives shadow games against a coordinator + mock control service.
type Runner struct {
	cfg    Config
	log    *slog.Logger
	tracer trace.Tracer
	dial   dialer
	// onTerminal, when set, is called once an experiment game reaches a terminal
	// outcome. nil ⇒ no callback (the #778 one-shot runner does not use it).
	onTerminal TerminalCallback
}

// New builds a Runner with the given config. log may be nil (slog.Default used).
func New(cfg Config, log *slog.Logger) *Runner {
	if log == nil {
		log = slog.Default()
	}
	return &Runner{
		cfg:    cfg.withDefaults(),
		log:    log,
		tracer: otel.Tracer(tracerName),
		dial:   defaultDialer,
	}
}

// SetTerminalCallback registers the #1014 terminal-outcome hook. It is invoked by
// the background drive goroutine of StartExperimentGame the moment the game ends
// (any outcome). The experiment loop wires it to release the in-flight slot for a
// non-concluding outcome so the cohort loop cannot deadlock.
func (r *Runner) SetTerminalCallback(cb TerminalCallback) { r.onTerminal = cb }

// clients bundles the two gRPC clients a run needs plus their cleanup.
type clients struct {
	coord gcpb.GameCoordinatorServiceClient
	mock  mockpb.MockControllerServiceClient
	close func()
}

// connect dials both endpoints and returns the clients. The returned close
// must be called when the run finishes.
func (r *Runner) connect() (*clients, error) {
	coordConn, closeCoord, err := r.dial(r.cfg.CoordinatorAddr)
	if err != nil {
		return nil, fmt.Errorf("dial coordinator %s: %w", r.cfg.CoordinatorAddr, err)
	}
	mockConn, closeMock, err := r.dial(r.cfg.MockAddr)
	if err != nil {
		_ = closeCoord()
		return nil, fmt.Errorf("dial mock service %s: %w", r.cfg.MockAddr, err)
	}
	return &clients{
		coord: gcpb.NewGameCoordinatorServiceClient(coordConn),
		mock:  mockpb.NewMockControllerServiceClient(mockConn),
		close: func() {
			_ = closeMock()
			_ = closeCoord()
		},
	}, nil
}

// RunShadowGame runs one mock-only game end-to-end and returns its result.
//
// The flow is: reserve controllers -> headless start -> capture game_id ->
// drive to win condition -> await terminal event (force-end on timeout) ->
// ALWAYS remove the reserved controllers. The reserved controllers are removed
// even when an error occurs partway through, so a failed run never orphans
// controllers (the tag-based SweepOrphans is the backstop for an agent crash).
func (r *Runner) RunShadowGame(ctx context.Context, spec Spec) (Result, error) {
	start := time.Now()
	result := Result{GameName: spec.GameName, Outcome: OutcomeError}

	if spec.RunID == "" {
		return result, errors.New("gamerunner: spec.RunID is required")
	}
	if spec.GameName == "" {
		return result, errors.New("gamerunner: spec.GameName is required")
	}
	if spec.Players < 2 {
		return result, fmt.Errorf("gamerunner: spec.Players must be >= 2, got %d", spec.Players)
	}

	ctx, span := r.tracer.Start(ctx, "agent.shadow_game.run", trace.WithAttributes(
		attribute.String("shadow_game.run_id", spec.RunID),
		attribute.String("shadow_game.mode", spec.GameName),
		attribute.Int("shadow_game.players", spec.Players),
		attribute.Int("shadow_game.sensitivity", spec.Sensitivity),
	))
	defer span.End()

	cl, err := r.connect()
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "connect failed")
		return result, err
	}
	defer cl.close()

	res, err := r.run(ctx, cl, spec, span)
	res.GameName = spec.GameName
	res.Duration = time.Since(start)
	if res.GameID != "" {
		span.SetAttributes(attribute.String("shadow_game.game_id", res.GameID))
	}
	span.SetAttributes(
		attribute.String("shadow_game.outcome", string(res.Outcome)),
		attribute.String("shadow_game.terminal_event", res.TerminalEvent),
		attribute.Int("shadow_game.events_seen", res.EventsSeen),
		attribute.Int64("shadow_game.duration_ms", res.Duration.Milliseconds()),
	)
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "shadow game failed")
	} else {
		span.SetStatus(codes.Ok, "")
	}
	return res, err
}

// StartExperimentGame starts ONE experiment-bound shadow game and returns its
// coordinator-assigned game_id PROMPTLY, then drives the game to completion in a
// background goroutine bounded by driveCtx. It is the async spawn the experiment
// registry (#978/#991) needs: Spawn must return the game_id under the registry's
// allocate tick, but the game itself plays out over minutes — and its CONCLUSION
// (fitness) flows back through the telemetry/OnGameEnd path, not this call.
//
// startCtx bounds only the reserve + start-handshake (the caller's tick context);
// driveCtx (the agent root context) bounds the background drive + await + cleanup,
// so the game is NOT killed when the tick returns — only at agent shutdown, which
// cancels driveCtx and lets the background goroutine force-end the game and ALWAYS
// remove its reserved controllers (the leak-safe contract). On a start error no
// goroutine is spawned and no controllers are left reserved.
func (r *Runner) StartExperimentGame(startCtx, driveCtx context.Context, spec Spec) (string, error) {
	if spec.RunID == "" {
		return "", errors.New("gamerunner: spec.RunID is required")
	}
	if spec.GameName == "" {
		return "", errors.New("gamerunner: spec.GameName is required")
	}
	if spec.Players < 2 {
		return "", fmt.Errorf("gamerunner: spec.Players must be >= 2, got %d", spec.Players)
	}

	cl, err := r.connect()
	if err != nil {
		return "", err
	}

	// Reserve controllers under the start context. On any failure before the
	// background goroutine takes ownership, we must clean up here so nothing leaks.
	serials, err := r.reserveControllers(startCtx, cl, spec)
	if err != nil {
		cl.close()
		return "", err
	}

	stream, gameID, err := r.startAndCapture(startCtx, cl, spec, serials)
	if err != nil {
		// Start failed: remove the controllers we reserved and close the clients.
		cleanupCtx, cancel := context.WithTimeout(context.Background(), r.cfg.RPCTimeout)
		r.removeControllers(cleanupCtx, cl, serials)
		cancel()
		cl.close()
		return "", err
	}

	// The game is live with a known id. Hand ownership of the drive/await/cleanup to
	// a background goroutine bounded by driveCtx. It ALWAYS removes the reserved
	// controllers and closes the clients on the way out (success, timeout, or
	// shutdown), so no controller or connection leaks.
	go func() {
		// #1014 deadlock backstop: the WHOLE point of onTerminal is to guarantee the
		// registry's in-flight slot is released even on a failed game, so the release
		// must itself be panic-proof. Fire it from a deferred closure that defaults to
		// OutcomeError and is updated to the real outcome once known — so a panic or
		// any future early-return in driveGame/awaitTerminal still releases the slot
		// (a non-concluding OutcomeError release, never a lost slot). Registered FIRST
		// so LIFO runs it LAST — after controller removal and client close — which
		// means the registry slot is released after this run's controllers are gone.
		// onTerminal synchronously kicks off the NEXT game's reserve+start; that next
		// reservation can therefore race ahead of game N's already-completed
		// removeControllers, but each run reserves its OWN uniquely-tagged controllers
		// (Spec.Tag = "agent:<RunID>"), so there is no controller-identity collision.
		finalOutcome := OutcomeError
		defer func() {
			if r.onTerminal != nil {
				r.onTerminal(gameID, finalOutcome)
			}
		}()

		defer cl.close()
		defer func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), r.cfg.RPCTimeout)
			r.removeControllers(cleanupCtx, cl, serials)
			cancel()
		}()

		runDrive, stopDrive := context.WithCancel(driveCtx)
		defer stopDrive()
		go r.driveGame(runDrive, cl, serials)

		terminal, _, outcome := r.awaitTerminal(driveCtx, cl, stream, gameID)
		finalOutcome = outcome
		stopDrive()
		r.log.Info("experiment shadow game finished",
			"game_id", gameID, "run_id", spec.RunID, "experiment_id", spec.ExperimentID,
			"arm", spec.Arm, "outcome", outcome, "terminal_event", terminal)
	}()

	return gameID, nil
}

// run is the core flow, assuming clients are connected. Cleanup of reserved
// controllers is unconditional (deferred) so an error after reservation never
// leaks controllers.
func (r *Runner) run(ctx context.Context, cl *clients, spec Spec, span trace.Span) (Result, error) {
	result := Result{GameName: spec.GameName, Outcome: OutcomeError}

	// (a) Reserve mock controllers, hidden from the menu via reserved+tag.
	serials, err := r.reserveControllers(ctx, cl, spec)
	if err != nil {
		return result, err
	}
	result.Serials = serials
	span.AddEvent("controllers_reserved", trace.WithAttributes(
		attribute.StringSlice("shadow_game.serials", serials),
	))

	// ALWAYS remove the reserved controllers, success or failure. Uses a fresh
	// context so cleanup still runs if ctx was cancelled (timeout / shutdown).
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), r.cfg.RPCTimeout)
		defer cancel()
		r.removeControllers(cleanupCtx, cl, serials)
		span.AddEvent("controllers_removed", trace.WithAttributes(
			attribute.Int("shadow_game.removed_count", len(serials)),
		))
	}()

	// (b+c) Headless start + capture game_id from the first stamped event.
	stream, gameID, err := r.startAndCapture(ctx, cl, spec, serials)
	if err != nil {
		return result, err
	}
	result.GameID = gameID
	r.log.Info("shadow game started", "run_id", spec.RunID, "game_id", gameID, "mode", spec.GameName)

	// (d) Drive the game to its win condition in the background while (e) we
	// await the terminal event on the stream.
	driveCtx, stopDrive := context.WithCancel(ctx)
	defer stopDrive()
	go r.driveGame(driveCtx, cl, serials)

	// (e) Await a terminal event; force-end on timeout.
	terminal, eventsSeen, outcome := r.awaitTerminal(ctx, cl, stream, gameID)
	result.EventsSeen = eventsSeen
	result.TerminalEvent = terminal
	result.Outcome = outcome
	stopDrive()

	if outcome == OutcomeError && terminal == "" {
		return result, fmt.Errorf("shadow game %s ended without a terminal event", gameID)
	}
	return result, nil
}
