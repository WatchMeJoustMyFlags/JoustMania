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
}

// Default config values. Conservative and CI-friendly.
const (
	DefaultCoordinatorAddr  = "game-coordinator:50053"
	DefaultMockAddr         = "controller-manager:50062"
	DefaultGameTimeout      = 90 * time.Second
	DefaultMovementInterval = 250 * time.Millisecond
	DefaultKillInterval     = 1500 * time.Millisecond
	DefaultRPCTimeout       = 10 * time.Second
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

// Runner drives shadow games against a coordinator + mock control service.
type Runner struct {
	cfg    Config
	log    *slog.Logger
	tracer trace.Tracer
	dial   dialer
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
