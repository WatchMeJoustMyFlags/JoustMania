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
//  3. lets the archetype's scripted motion run: it is installed when the
//     controllers are reserved (shadow games are archetype-only, UC-046), and
//     the runner sends no movement or kill RPCs (BR-004),
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
	"github.com/joustmania/agent/patterngame"
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
	// OutcomeForceEnded means the game was force-ended without a usable sample:
	// the runner force-ended it for the kill switch (UC-046 A12), or the
	// coordinator force-ended it for a reason the runner did not request (a
	// foreign force-end, UC-046 BR-016) other than a real-game preemption
	// (OutcomePreempted) or the agent's own end_game (OutcomeAgentEnded). The
	// experiment releases it, and its pair's rerun counts against BR-015's budget.
	OutcomeForceEnded Outcome = "force_ended"
	// OutcomePreempted means a real game preempted the shadow game (coordinator
	// reason preempted_by_real_game, UC-046 A8): released without a sample like
	// OutcomeForceEnded, but its pair's rerun does not use up BR-015's budget.
	OutcomePreempted Outcome = "preempted"
	// OutcomeAgentEnded means the agent's own end_game intervention force-ended
	// the game (coordinator reason agent_intervention, UC-046 A14): a VALID,
	// right-censored result whose fitness sample counts, like OutcomeTimeout.
	OutcomeAgentEnded Outcome = "agent_ended"
	// OutcomeError means the game emitted game_error, or the run failed before
	// reaching a terminal event.
	OutcomeError Outcome = "error"
	// OutcomeTimeout means a pattern game reached its safety cap and was
	// force-ended (UC-046 A3, BR-013): a VALID result whose fitness sample counts.
	OutcomeTimeout Outcome = "timeout"
)

// patternOutcome maps a run outcome to the replay record's pattern.outcome.
func patternOutcome(o Outcome) string {
	switch o {
	case OutcomeCompleted:
		return patterngame.OutcomeWinner
	case OutcomeTimeout:
		return patterngame.OutcomeTimeout
	case OutcomeAgentEnded:
		return patterngame.OutcomeAgentEnded
	case OutcomeForceEnded, OutcomePreempted:
		return patterngame.OutcomeAborted
	default:
		return patterngame.OutcomeError
	}
}

// Config holds the endpoints and tuning for a Runner. Endpoints mirror the
// docker-compose service names; the zero value is filled by ConfigFromEnv.
type Config struct {
	// CoordinatorAddr is the GameCoordinatorService gRPC endpoint
	// (docker-compose: game-coordinator:50053).
	CoordinatorAddr string
	// MockAddr is the MockControllerService gRPC endpoint. The mock control API
	// rides on the controller-manager container, port 50062.
	MockAddr string

	// RPCTimeout bounds individual unary RPCs (AddControllers, ForceEndGame, …).
	// Every shadow game is a pattern game (UC-046): its await bound is the
	// archetype's pinned safety cap (Spec.MaxGameSeconds), and no movement or kill
	// commands are ever sent.
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
	DefaultCoordinatorAddr = "game-coordinator:50053"
	DefaultMockAddr        = "controller-manager:50062"
	DefaultRPCTimeout      = 10 * time.Second
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

	// ExperimentSpanContext is the SpanContext of the agent's long-lived
	// EXPERIMENT root span (#1182, epic #1181). When set (Valid), an
	// experiment-bound shadow spawn injects it as the OUTGOING traceparent on the
	// StreamGameEvents start call, so the coordinator's game-lifecycle span is
	// created as a CHILD of the experiment span (causal parent-child) rather than
	// its own root. The zero (invalid) value — a real game, an unbound shadow
	// game, or an experiment with no recordable root span — injects nothing, so
	// the game stays own-rooted exactly as before (graceful).
	ExperimentSpanContext trace.SpanContext

	// Seed is the deterministic RNG seed for paired CRN shadow games (#1003).
	// When set (non-zero), it is threaded onto StartGameConfig.rng_seed so the
	// game-coordinator constructs the game's per-instance RNG from it — the two
	// games in a (experimental, control) pair share a Seed so they face an
	// identical bracket order / role assignment / tempo schedule. 0 (the default)
	// means entropy: an unbound shadow game or the #778 one-shot runner sets
	// nothing, byte-unchanged from before. Honored coordinator-side ONLY for a
	// shadow session (the hard real-protection invariant, epic #982 decision 7).
	Seed uint64

	// Pattern, when Pattern.Name is set, makes this a pattern game (UC-046): the
	// controllers are reserved with the archetype installed (Players = its players),
	// the runner sends no SimulateMovement / SimulateDeath (BR-004), force-ends the
	// game at the safety cap MaxGameSeconds with outcome timeout (BR-013), and
	// builds the replay record from the controllers' RemoveController answers.
	Pattern patterngame.Archetype
	// MaxGameSeconds is the pinned safety cap of a pattern game (0 = default 300 s).
	MaxGameSeconds int
	// FlagDigest is UC-046 BR-011's digest of the flag files at spawn.
	FlagDigest string
	// PairIndex is the experiment pair (-1 for a one-shot game).
	PairIndex int
}

// IsPattern reports whether the spec runs a pattern game.
func (s Spec) IsPattern() bool { return s.Pattern.Name != "" }

// gameTimeout is the await bound: the pinned safety cap (UC-046 BR-013; the unit
// is one second in production, tests shrink it).
func (s Spec) gameTimeout(unit time.Duration) time.Duration {
	return time.Duration(patterngame.ClampCap(s.MaxGameSeconds)) * unit
}

// errNotPattern refuses a shadow game without an archetype: shadow games are
// archetype-only (UC-046, maintainer decision on #1258), so no random-motion /
// scripted-kill game can ever be started from the agent.
var errNotPattern = errors.New("gamerunner: shadow games need a motion archetype (UC-046: experiments are archetype-only)")

// validatePatternSpec checks what every shadow game needs.
func validatePatternSpec(spec Spec) error {
	switch {
	case spec.RunID == "":
		return errors.New("gamerunner: spec.RunID is required")
	case spec.GameName == "":
		return errors.New("gamerunner: spec.GameName is required")
	case !spec.IsPattern():
		return errNotPattern
	case spec.GameName != patterngame.SupportedMode:
		return fmt.Errorf("gamerunner: %s: mode %s (pattern games are %s only)",
			patterngame.DiscardModeUnsupported, spec.GameName, patterngame.SupportedMode)
	case spec.Players < 2:
		return fmt.Errorf("gamerunner: spec.Players must be >= 2, got %d", spec.Players)
	}
	return nil
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
	// Pattern is the replay record of a pattern game (nil otherwise).
	Pattern *patterngame.Record
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

// PatternCallback receives the replay record of a finished pattern game (UC-046
// step 8). It is called after the controllers are removed and before the
// TerminalCallback.
type PatternCallback func(rec patterngame.Record)

// PatternStartCallback receives a pattern game's registration (UC-046 step 3) as
// soon as its game_id is known.
type PatternStartCallback func(info patterngame.Info)

// Runner drives shadow games against a coordinator + mock control service.
type Runner struct {
	cfg    Config
	log    *slog.Logger
	tracer trace.Tracer
	dial   dialer
	// onTerminal, when set, is called once an experiment game reaches a terminal
	// outcome. nil ⇒ no callback (the #778 one-shot runner does not use it).
	onTerminal TerminalCallback
	onPattern  PatternCallback
	onStart    PatternStartCallback
	tracker    *patterngame.Tracker
	// capUnit scales the pattern safety cap (time.Second; tests shrink it).
	capUnit time.Duration
	// endedGrace bounds the wait for a game_force_ended after a game_ended
	// without an outcome (UC-046 BR-016; DefaultEndedGrace, tests shrink it).
	endedGrace time.Duration
}

// SetPatternCallback registers the UC-046 replay-record hook.
func (r *Runner) SetPatternCallback(cb PatternCallback) { r.onPattern = cb }

// SetPatternStartCallback registers the UC-046 game-start hook.
func (r *Runner) SetPatternStartCallback(cb PatternStartCallback) { r.onStart = cb }

// SetTracker overrides the pattern-game tracker (tests); default patterngame.Default.
func (r *Runner) SetTracker(t *patterngame.Tracker) { r.tracker = t }

// New builds a Runner with the given config. log may be nil (slog.Default used).
func New(cfg Config, log *slog.Logger) *Runner {
	if log == nil {
		log = slog.Default()
	}
	return &Runner{
		cfg:        cfg.withDefaults(),
		log:        log,
		tracer:     otel.Tracer(tracerName),
		dial:       defaultDialer,
		tracker:    patterngame.Default,
		capUnit:    time.Second,
		endedGrace: DefaultEndedGrace,
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

	spec = withPatternPlayers(spec)
	if err := validatePatternSpec(spec); err != nil {
		return result, err
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
		// shadow_game.game_id is the run-domain key; game.id (#1088) is the SAME
		// game_<uuid> under the canonical, cross-family key so a Jaeger query by
		// game.id correlates this run span with the agent's decision/llm spans and
		// the coordinator's per-game trace.
		span.SetAttributes(
			attribute.String("shadow_game.game_id", res.GameID),
			attribute.String("game.id", res.GameID),
		)
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
	spec = withPatternPlayers(spec)
	if err := validatePatternSpec(spec); err != nil {
		return "", err
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
		// any future early-return in awaitTerminal still releases the slot
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
		abortCh, _, _ := r.registerPattern(spec, gameID, serials)
		watcher := &eventWatcher{}
		defer func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), r.cfg.RPCTimeout)
			slots := r.removeControllers(cleanupCtx, cl, serials)
			cancel()
			if rec, ok := r.finishPattern(spec, gameID, finalOutcome, watcher.get(), slots); ok && r.onPattern != nil {
				r.onPattern(rec)
			}
		}()

		// No drive: a pattern game's players die organically (UC-046 BR-004).
		terminal, _, outcome := r.awaitTerminal(driveCtx, cl, stream, gameID, spec.gameTimeout(r.capUnit), abortCh, watcher.observe, r.livenessTick(gameID))
		finalOutcome = outcome
		r.log.Debug("experiment shadow game finished",
			"game_id", gameID, "run_id", spec.RunID, "experiment_id", spec.ExperimentID,
			"arm", spec.Arm, "outcome", outcome, "terminal_event", terminal)
	}()

	return gameID, nil
}

// run is the core flow, assuming clients are connected. Cleanup of reserved
// controllers is unconditional (deferred) so an error after reservation never
// leaks controllers.
func (r *Runner) run(ctx context.Context, cl *clients, spec Spec, span trace.Span) (result Result, err error) {
	result = Result{GameName: spec.GameName, Outcome: OutcomeError}

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
	gameID := ""
	watcher := &eventWatcher{}
	defer func() {
		cleanupCtx, cancel := context.WithTimeout(context.Background(), r.cfg.RPCTimeout)
		defer cancel()
		slots := r.removeControllers(cleanupCtx, cl, serials)
		span.AddEvent("controllers_removed", trace.WithAttributes(
			attribute.Int("shadow_game.removed_count", len(serials)),
		))
		if rec, ok := r.finishPattern(spec, gameID, result.Outcome, watcher.get(), slots); ok {
			result.Pattern = &rec
			addPatternEndedEvent(span, rec)
			if r.onPattern != nil {
				r.onPattern(rec)
			}
		}
	}()

	// (b+c) Headless start + capture game_id from the first stamped event.
	stream, gameID, err := r.startAndCapture(ctx, cl, spec, serials)
	if err != nil {
		return result, err
	}
	result.GameID = gameID
	r.log.Debug("shadow game started", "run_id", spec.RunID, "game_id", gameID, "mode", spec.GameName)
	abortCh, info, isPattern := r.registerPattern(spec, gameID, serials)
	if isPattern {
		span.AddEvent("experiment.pattern_game", trace.WithAttributes(PatternGameAttributes(info)...))
	}

	// (d) No drive: a pattern game's players die organically (UC-046 BR-004).
	// (e) Await a terminal event; force-end at the safety cap.
	terminal, eventsSeen, outcome := r.awaitTerminal(ctx, cl, stream, gameID, spec.gameTimeout(r.capUnit), abortCh, watcher.observe, r.livenessTick(gameID))
	result.EventsSeen = eventsSeen
	result.TerminalEvent = terminal
	result.Outcome = outcome

	if outcome == OutcomeError && terminal == "" {
		return result, fmt.Errorf("shadow game %s ended without a terminal event", gameID)
	}
	return result, nil
}
