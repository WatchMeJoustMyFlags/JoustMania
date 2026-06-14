package main

import (
	"context"
	"io"
	"log/slog"
	"net"
	"testing"
	"time"

	"go.uber.org/goleak"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/infracontext"
)

// server_shutdown_test.go verifies the agent's graceful-shutdown path (#923): run()
// owns every runtime goroutine (the OTLP gRPC receiver, the health server, the
// per-game decision LoopSet, the eviction ticker, and the shared inference
// resolver's background probe ticker), and on ctx cancellation each MUST terminate
// without leaking a goroutine. go.uber.org/goleak makes the no-leak guarantee an
// assertion: if any of those loops hung (e.g. the eviction loop stopped selecting
// on ctx.Done(), or run returned before draining the shutdown goroutine), goleak
// would find the survivor and fail the test.

// leakOptions returns the goleak options each shutdown test uses. The deterministic
// core is goleak.IgnoreCurrent(): captured at the START of every test (the deferred
// call's arguments are evaluated when the defer statement runs, before run() spawns
// anything), it snapshots the goroutine set that is ALREADY running — package-init
// and other-test process-global singletons such as the OpenFeature event executor —
// and excludes exactly those. The leak check therefore measures only the DELTA that
// run() introduced, which is order-independent and resilient to library-internal
// symbol renames.
//
// This is the fix for #1036: these tests failed non-race because the OpenFeature
// event-executor singleton (started lazily and never stopped by run() — only by
// openfeature.Shutdown(), which main() owns) was a survivor goleak counted. The
// previous static IgnoreTopFunction string for it had drifted from the SDK's actual
// function name ("newEventExecutor." prefix that does not exist, one nesting level
// off) and so silently matched nothing.
//
// The static IgnoreTopFunction/IgnoreAnyFunction entries below are kept as a
// belt-and-braces backstop for the same process-global singletons in case one is
// spawned lazily AFTER the baseline snapshot is taken (none should be, given these
// tests omit flagd/OTEL, but it costs nothing):
//
//   - the OpenFeature SDK's event executor: a package-level singleton started on
//     first API access, torn down only by openfeature.Shutdown(), which main() owns
//     via a deferred shutdownFlags, NOT run(). The top-of-stack is the inner
//     listener goroutine spawned inside startEventListener.
//   - gRPC's balancer watcher / connection-pool goroutines: lazily created package
//     singletons, not goroutines run() spawns.
//   - go.opencensus.io's stats view worker: a package-level singleton started
//     transitively when other tests construct the OTLP / gcp exporters; run() never
//     starts it and cannot stop it.
//
// Everything run() DOES own (eviction loop, resolver probe ticker, shutdown
// goroutine, health + gRPC servers) is spawned AFTER the baseline and is NOT in the
// static ignore list, so the test still fails if any of those leaks.
func leakOptions() []goleak.Option {
	return []goleak.Option{
		// Baseline: everything already running before run() starts.
		goleak.IgnoreCurrent(),
		goleak.IgnoreTopFunction("github.com/open-feature/go-sdk/openfeature.(*eventExecutor).startEventListener.func1.1"),
		goleak.IgnoreTopFunction("google.golang.org/grpc.(*ccBalancerWrapper).watcher"),
		goleak.IgnoreAnyFunction("go.opencensus.io/stats/view.(*worker).start"),
	}
}

// freePort grabs an ephemeral TCP port and frees it, returning a ":<port>" address
// run can bind. Using a fresh port per test avoids a bind clash with any other
// listener on a shared CI box.
func freePort(t *testing.T) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("reserve port: %v", err)
	}
	addr := lis.Addr().String()
	_ = lis.Close()
	return addr
}

// newShutdownComponents builds a serverComponents wired with the real runtime
// pieces whose goroutine lifecycle the shutdown test exercises: a multiplexer, a
// LoopSet, an infra store, a pipeline, and a real inference resolver (its probe
// ticker is the background goroutine most likely to leak if run() stopped owning
// it). It deliberately omits flagd/OTEL — those providers spawn background
// transport goroutines outside this package's control, and run() does not own them
// (main() does, via deferred shutdowns), so they are not part of the run() drain
// contract this test asserts.
func newShutdownComponents(t *testing.T) *serverComponents {
	t.Helper()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, nil)
	})
	loops := decision.NewLoopSet(func(string) *decision.Loop {
		return decision.NewLoop(nil, logger)
	})
	infraStore := infracontext.NewStore(time.Hour, nil)
	pipe := newPipeline(mux, loops, time.Hour).withInfra(infraStore, nil)

	// A real resolver with endpoints that never resolve in a test (so every probe
	// fails fast) and a short probe interval so the ticker actually fires at least
	// once during the test, proving it stops on ctx cancellation rather than merely
	// never having started. Its background ticker is started by run().
	resolver := decision.NewResolver(decision.DefaultChain(decision.Endpoints{
		Cloud:     "",
		Jetson:    "192.0.2.1:1", // TEST-NET-1, guaranteed unroutable
		Localhost: "127.0.0.1:0", // never a listener
	}), 5*time.Millisecond)

	return &serverComponents{
		listenAddr:    freePort(t),
		healthAddr:    freePort(t),
		logger:        logger,
		pipe:          pipe,
		mux:           mux,
		loops:         loops,
		resolver:      resolver,
		evictInterval: 5 * time.Millisecond, // tick frequently so the loop is actually exercised
	}
}

// TestRun_GracefulShutdownNoGoroutineLeak is the headline #923 assertion: start the
// agent server core, let its goroutines run for real (the eviction ticker and the
// resolver probe ticker both fire), then cancel the context (the SIGTERM analogue)
// and assert run() returns AND no goroutine leaks.
//
// goleak.VerifyNone runs at the END of the test body, after run() has returned, so
// it sees the steady-state goroutine set. If the eviction loop, the resolver
// ticker, or the shutdown goroutine had failed to terminate on ctx cancellation,
// goleak would report the survivor with its stack and fail — exactly the leak this
// test is meant to catch.
func TestRun_GracefulShutdownNoGoroutineLeak(t *testing.T) {
	// leakOptions()'s IgnoreCurrent() snapshot is taken here (deferred args evaluate
	// now, before run() spawns anything), so the leak check asserts only on run()'s
	// own goroutines. See leakOptions for the #1036 rationale.
	defer goleak.VerifyNone(t, leakOptions()...)

	c := newShutdownComponents(t)

	ctx, cancel := context.WithCancel(context.Background())

	var (
		runErr  error
		runDone = make(chan struct{})
	)
	go func() {
		defer close(runDone)
		runErr = c.run(ctx)
	}()

	// Wait until the server is actually accepting connections, so the start path
	// (every goroutine spawned) has fully run before we cancel — otherwise we might
	// cancel before run() even spawned its loops and trivially "leak nothing".
	waitDial(t, c.listenAddr)

	// Let the tickers fire a few times so we are exercising the running loops, not a
	// just-started server. The resolver probe interval and eviction interval are both
	// 5ms, so this is several ticks.
	time.Sleep(50 * time.Millisecond)

	// The SIGTERM analogue: in main() signal.NotifyContext cancels ctx; here we cancel
	// directly. run()'s shutdown goroutine is unblocked, GracefulStop unblocks Serve,
	// and every loop selecting on ctx.Done() returns.
	cancel()

	select {
	case <-runDone:
	case <-time.After(10 * time.Second):
		t.Fatal("run() did not return within 10s of context cancellation (shutdown hung)")
	}
	if runErr != nil {
		t.Fatalf("run() returned error: %v", runErr)
	}
	// goleak.VerifyNone (deferred) now asserts no goroutine outlived run().
}

// TestRun_ShutdownCancelsContextBoundWork proves the contract that makes the #917
// async-inference no-leak guarantee hold at the run() level: the context run() is
// driven by is the SAME context that bounds every fired inference goroutine (main()
// passes it to each Loop via SetRootContext, and the same ctx cancels run). So when
// run's shutdown path fires, that context is already cancelled, every in-flight
// goroutine's latency-budget context is cancelled with it, and LoopSet.AwaitInflight
// (called in run's shutdown path) joins them without hanging.
//
// We model an in-flight inference with a goroutine bounded by the SAME ctx run is
// driven by — exactly the rootCtx relationship — and assert run() does not return,
// nor does goleak pass, until that goroutine has observed the cancellation and
// exited. If run's shutdown failed to let ctx-cancellation propagate (or returned
// while context-bound work was still running), goleak would catch the survivor.
func TestRun_ShutdownCancelsContextBoundWork(t *testing.T) {
	// The IgnoreCurrent() baseline inside leakOptions() is captured HERE (deferred
	// args evaluate at the defer statement), which is BEFORE the context-bound
	// inference analogue goroutine is spawned below — so that goroutine is NOT
	// baselined-out and goleak still requires it to have exited on shutdown.
	defer goleak.VerifyNone(t, leakOptions()...)

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, nil)
	})

	ctx, cancel := context.WithCancel(context.Background())

	// An in-flight-inference analogue bounded by the SAME root ctx run() uses: it
	// blocks until ctx is cancelled, then exits — exactly how a well-behaved Infer
	// returns when its rootCtx-derived timeout context is cancelled at shutdown.
	gateExited := make(chan struct{})
	go func() {
		defer close(gateExited)
		<-ctx.Done()
	}()

	loops := decision.NewLoopSet(func(string) *decision.Loop {
		return decision.NewLoop(nil, logger)
	})
	infraStore := infracontext.NewStore(time.Hour, nil)
	pipe := newPipeline(mux, loops, time.Hour).withInfra(infraStore, nil)

	c := &serverComponents{
		listenAddr:    freePort(t),
		healthAddr:    freePort(t),
		logger:        logger,
		pipe:          pipe,
		mux:           mux,
		loops:         loops,
		evictInterval: 5 * time.Millisecond,
	}

	runDone := make(chan struct{})
	go func() {
		defer close(runDone)
		_ = c.run(ctx)
	}()
	waitDial(t, c.listenAddr)

	cancel()

	select {
	case <-runDone:
	case <-time.After(10 * time.Second):
		t.Fatal("run() did not return after cancellation")
	}
	// The context-bound work must have observed cancellation and exited.
	select {
	case <-gateExited:
	case <-time.After(2 * time.Second):
		t.Fatal("context-bound inference analogue did not exit on shutdown (leak)")
	}
}

// waitDial blocks until addr accepts a TCP connection (the gRPC server is up) or
// the timeout elapses, so a test cancels only after the start path fully ran.
func waitDial(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		conn, err := net.DialTimeout("tcp", addr, 200*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("server at %s never came up", addr)
}
