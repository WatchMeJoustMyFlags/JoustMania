package main

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	"google.golang.org/grpc"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
)

// serverComponents bundles the already-constructed runtime pieces that the agent
// server orchestration (run) owns the lifecycle of. It is the testability seam for
// the graceful-shutdown verification (#923): main() builds every env-gated
// dependency and hands the goroutine-owning core to run, while a test can construct
// a minimal set (no flagd/OTEL background machinery) and drive start -> cancel ->
// drain to assert no goroutine leaks.
//
// Everything here is the part of main() that SPAWNS or STOPS goroutines:
//   - the gRPC OTLP receiver (GracefulStop on shutdown),
//   - the health HTTP server (Shutdown on shutdown),
//   - the per-game decision LoopSet (AwaitInflight joins in-flight remote decide calls),
//   - the GameContext multiplexer + its eviction ticker loop.
//
// The env-gated construction (flagd, OTEL, action sink, promoter, shadow game,
// proposer, ...) deliberately stays in main(): it is wiring, not a goroutine the
// run loop must drain, and the flagd/OTEL providers spawn background transport
// goroutines outside this package's control that the leak test must not have to
// reason about.
type serverComponents struct {
	listenAddr string
	healthAddr string
	logger     *slog.Logger

	pipe  *pipeline
	mux   *gamecontext.Multiplexer
	loops *decision.LoopSet

	// retro is the post-game RetroCoordinator. Its async inference goroutines (#1179)
	// are joined on shutdown via AwaitInflight so no retro outlives the process. May be
	// nil (tests that do not exercise the retro path).
	retro *decision.RetroCoordinator

	// predictions is the prediction book (#2479); its horizon timer goroutines are
	// bounded by the root context and joined on shutdown. May be nil (tests).
	predictions interface{ Await() }

	// evictInterval is the eviction ticker cadence. main() hot-reloads it from the
	// lifecycle holder via evictIntervalSrc; tests pass a fixed value.
	evictInterval time.Duration
	// evictIntervalSrc, when non-nil, is the LIVE eviction-cadence source (#927):
	// after each tick run re-reads it and Resets the ticker if it changed. Nil keeps
	// the static evictInterval (the test path).
	evictIntervalSrc func() time.Duration

	// shadow, when non-nil, is invoked in its own ctx-bounded goroutine after the
	// servers come up (the #778 shadow-game runner in production), tracked on the
	// background group so shutdown joins it (#2318). Nil = no shadow game (tests, and
	// the unset env default).
	shadow func(context.Context)

	// dossier, when non-nil, is the read-only experiment-dossier reader (#1183). Its
	// GET /experiments[/{id}] routes are mounted on the health HTTP server below so the
	// dossier shares the agent's lifecycle with no extra port. Nil leaves the health
	// server serving only /healthz (the test path and the pre-#1183 behavior).
	dossier *dossierReader

	// experiments, when non-nil, runs the #991 experiment cohort loop in its own
	// ctx-bounded goroutine: declare → spawn shadow games into arms → conclude →
	// (gated) promote. Nil = no experiment loop (test wiring; production always
	// builds it and gates it on the live experiments_enabled flag), in which case the agent has zero experiment surface and
	// behaves exactly as before. run() joins this goroutine on shutdown so it is
	// leak-safe (the loop's own AbortAll tears experiments down on ctx cancel).
	experiments func(context.Context)

	// background, when non-nil, holds the fire-and-forget goroutines the wiring
	// starts outside run() (the game-end proposer, #2323); run() adds the shadow game
	// (#2318). run()'s shutdown path closes and joins it after the gRPC server has
	// stopped, so none of them outlives run() and races the deferred tracer / metrics
	// shutdown. Nil = run() uses a group of its own for the shadow game.
	background *backgroundGroup
}

// effectiveEvictInterval returns the live eviction cadence when a source is wired
// and positive, else the static construction value.
func (c *serverComponents) effectiveEvictInterval() time.Duration {
	if c.evictIntervalSrc != nil {
		if d := c.evictIntervalSrc(); d > 0 {
			return d
		}
	}
	return c.evictInterval
}

// run is the agent's server orchestration loop, extracted from main() so the
// start -> cancel -> drain path is testable (#923). It:
//
//  1. registers the OTLP trace + metrics receivers on a fresh gRPC server,
//  2. starts the health HTTP server,
//  3. runs the eviction ticker loop (evict stale partitions + drop their loops),
//  4. optionally fires the shadow game on the background group,
//  5. on ctx cancellation, GracefulStops the gRPC server, joins in-flight async
//     goroutines (remote decide calls, effect samplers, retros), and shuts the
//     health server down,
//  6. blocks on grpcServer.Serve and returns its error once the server has stopped.
//
// Every goroutine it spawns terminates on ctx cancellation: the eviction loop
// selects on ctx.Done(), and the shutdown goroutine is unblocked by ctx.Done() and then GracefulStop returns
// (unblocking Serve), so run returns and no goroutine outlives it. This is what the
// goleak-based shutdown test asserts.
func (c *serverComponents) run(ctx context.Context) error {
	grpcServer := grpc.NewServer()
	ptraceotlp.RegisterGRPCServer(grpcServer, &traceReceiver{pipe: c.pipe})
	pmetricotlp.RegisterGRPCServer(grpcServer, &metricsReceiver{pipe: c.pipe})

	lis, err := net.Listen("tcp", c.listenAddr)
	if err != nil {
		return err
	}

	healthServer := c.startHealthServer()
	stopEvict := c.startEvictionLoop(ctx)

	// Goroutines started outside run() (the game-end proposer, #2323) and the shadow
	// game (#2318) share one group that the shutdown path joins. Without a group from
	// the caller run() uses its own, so the shadow game is joined either way.
	background := c.background
	if background == nil {
		background = &backgroundGroup{}
	}
	if c.shadow != nil {
		background.Go(func() { c.shadow(ctx) })
	}

	// The #991 experiment cohort loop, when enabled (opt-in default-off). It runs
	// until ctx is cancelled, then AbortAll's its live experiments and returns. We
	// join it on shutdown (expDone) so no goroutine outlives run() — the same
	// leak-safe ownership the eviction/shutdown goroutines have.
	expDone := make(chan struct{})
	if c.experiments != nil {
		go func() {
			defer close(expDone)
			c.experiments(ctx)
		}()
	} else {
		close(expDone) // nothing to wait on when the loop is disabled.
	}

	// Graceful shutdown on ctx cancellation. GracefulStop unblocks Serve below; the
	// AwaitInflight join makes the #917 no-leak guarantee explicit (ctx is already
	// cancelled, so each fired call's timeout context is cancelled and a well-behaved
	// Infer returns promptly).
	shutdownDone := make(chan struct{})
	go func() {
		defer close(shutdownDone)
		<-ctx.Done()
		c.logger.Info("Shutdown requested, stopping servers...")
		grpcServer.GracefulStop()
		c.loops.AwaitInflight()
		// Join in-flight async retro inference goroutines (#1179) too: ctx (the agent
		// root) is already cancelled, and each retro goroutine bridges root.Done() to its
		// budget-context cancel (SetRootContext), so a well-behaved in-flight Infer is
		// unblocked promptly rather than waiting out the full latency budget; AwaitInflight
		// then joins them. No retro outlives the process.
		if c.retro != nil {
			c.retro.AwaitInflight()
		}
		// The prediction book's timers stop without scoring on ctx cancellation (#2479).
		if c.predictions != nil {
			c.predictions.Await()
		}
		// Join the game-end proposer goroutines (#2323) and the shadow game (#2318),
		// which ctx bounds. GracefulStop above ended the OTLP intake, so no new game end
		// arrives; Close also refuses a late one.
		background.Close()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = healthServer.Shutdown(shutdownCtx)
	}()

	c.logger.Info("JoustMania Agent listening (OTLP gRPC)", "addr", c.listenAddr)
	serveErr := grpcServer.Serve(lis)

	// Serve has returned (GracefulStop was called or it errored). Make sure the
	// shutdown goroutine and the eviction loop have both fully drained before we
	// return, so no goroutine this function spawned outlives it.
	<-shutdownDone
	<-stopEvict
	<-expDone
	return serveErr
}

// startHealthServer brings up the /healthz HTTP server in its own goroutine and
// returns the *http.Server so run's shutdown path can Shutdown it. A failure other
// than ErrServerClosed is logged (non-fatal: health is auxiliary).
func (c *serverComponents) startHealthServer() *http.Server {
	healthMux := http.NewServeMux()
	healthMux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("OK"))
	})
	// Mount the read-only experiment-dossier routes (#1183) on the same mux when a
	// reader is wired. GET-only, journal-off-disk reads — never blocks the agent loop.
	if c.dossier != nil {
		registerDossierRoutes(healthMux, c.dossier)
	}
	healthServer := &http.Server{Addr: c.healthAddr, Handler: healthMux}
	go func() {
		c.logger.Info("Health server listening", "addr", c.healthAddr)
		if err := healthServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			c.logger.Error("Health server failed", "error", err)
		}
	}()
	return healthServer
}

// startEvictionLoop runs the partition-eviction ticker in its own goroutine and
// returns a channel closed when the loop exits (on ctx cancellation). On each tick
// it evicts stale Store partitions, drops the matching decision Loops in lockstep
// (#845 PR C; the fallback loop is retained by Retain), evicts the infra store, and
// hot-reloads the cadence if the live source moved (#927).
func (c *serverComponents) startEvictionLoop(ctx context.Context) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		interval := c.effectiveEvictInterval()
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				// A stale game_active backstop end (UC-041 A4, #1494) runs the whole
				// OnGameEnd chain (stale counter, experiment release; retro, summary and
				// proposer skip it) synchronously on THIS goroutine, outside the store
				// and multiplexer locks, so the tick takes as long as that chain.
				c.mux.EvictStale()
				c.loops.Retain(partitionSet(c.mux.Partitions()))
				if c.pipe.infraStore != nil {
					c.pipe.infraStore.EvictStale()
				}
				if next := c.effectiveEvictInterval(); next > 0 && next != interval {
					interval = next
					ticker.Reset(interval)
				}
			}
		}
	}()
	return done
}
