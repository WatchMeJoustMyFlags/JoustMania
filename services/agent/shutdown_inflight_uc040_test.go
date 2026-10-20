package main

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/infracontext"
)

// slowDecideBackend is a remote backend that answers only once released, ignoring
// its context, so a call stays in flight past the shutdown.
type slowDecideBackend struct {
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (b *slowDecideBackend) Name() string    { return "claude" }
func (b *slowDecideBackend) Available() bool { return true }
func (b *slowDecideBackend) Decide(context.Context, decide.Request) (decide.Response, error) {
	b.once.Do(func() { close(b.started) })
	<-b.release
	return decide.Response{}, context.Canceled
}

type staticFlagSource struct{ snap flags.Snapshot }

func (s staticFlagSource) Evaluate(context.Context) flags.Snapshot { return s.snap }

type oneGameProvider struct{ c gamecontext.GameContext }

func (p oneGameProvider) Current(string) (gamecontext.GameContext, bool) { return p.c, true }

// TestRun_WaitsForInflightDecideBeforeReturning: main flushes the agent's spans in
// the deferred shutdownTracing, which runs only after run() returns. run() must
// therefore not return while a remote decide call is in flight, and the call's
// agent.decide span must have ended (discarded as shutdown) by the time it does.
// UC: UC-040 A29
func TestRun_WaitsForInflightDecideBeforeReturning(t *testing.T) {
	t.Run("UC-040/A29 the agent waits for in-flight calls before it flushes its spans", func(t *testing.T) {
		logger := slog.New(slog.NewTextHandler(io.Discard, nil))
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()

		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		defer func() { _ = tp.Shutdown(context.Background()) }()

		active := true
		game := gamecontext.GameContext{SessionID: "g-1", GameKind: "real", Players: map[string]*gamecontext.PlayerSignals{
			"A": {Serial: "A", Active: &active},
			"B": {Serial: "B", Active: &active},
		}}
		game.Session.GameActive = &active
		snap := flags.Snapshot{
			Enabled:              true,
			InterventionsAllowed: []string{"noop", "grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 100},
			LLMGate:              flags.LLMGate{EligibleGameKinds: []string{"real"}, MaxRequestsPerMinute: 100},
			Decision: flags.DecisionBackend{
				Backend:                flags.DecisionBackendClaude,
				ActConfidenceThreshold: 0.8,
				Claude:                 flags.RemoteBackendConfig{Model: "m", Timeout: time.Hour},
			},
		}
		be := &slowDecideBackend{started: make(chan struct{}), release: make(chan struct{})}

		loop := decision.NewLoop(staticFlagSource{snap: snap}, logger)
		loop.Tracer = tp.Tracer("test")
		loop.SetLLMBudget(decision.NewLLMBudget())
		loop.SetGameID("g-1")
		loop.SetContextProvider(oneGameProvider{c: game})
		loop.SetRootContext(ctx)
		reg := decision.NewDecideRegistry()
		reg.Register("claude", func(flags.DecisionBackend) decide.Backend { return be })
		loop.SetDecideRegistry(reg)

		loops := decision.NewLoopSet(func(string) *decision.Loop { return loop })
		loops.For("g-1")
		mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
			return gamecontext.NewStore(time.Hour, time.Hour, nil)
		})
		c := &serverComponents{
			listenAddr:    freePort(t),
			healthAddr:    freePort(t),
			logger:        logger,
			pipe:          newPipeline(mux, loops, time.Hour).withInfra(infracontext.NewStore(time.Hour, nil), nil),
			mux:           mux,
			loops:         loops,
			evictInterval: time.Hour,
		}
		runDone := make(chan struct{})
		go func() {
			defer close(runDone)
			_ = c.run(ctx)
		}()
		waitDial(t, c.listenAddr)

		loop.OnEvaluate(context.Background(), game, decision.EvalTrigger{Signal: "metrics", T0: time.Now()})
		select {
		case <-be.started:
		case <-time.After(5 * time.Second):
			t.Fatal("the remote decide call never started")
		}

		cancel() // SIGTERM
		select {
		case <-runDone:
			t.Fatal("run() returned while a decide call was in flight: main would flush spans before it ends")
		case <-time.After(300 * time.Millisecond):
		}
		if n := len(decideSpans(sr)); n != 0 {
			t.Fatalf("agent.decide spans ended before the call finished = %d, want 0", n)
		}

		close(be.release)
		select {
		case <-runDone:
		case <-time.After(10 * time.Second):
			t.Fatal("run() did not return after the in-flight call finished")
		}
		ds := decideSpans(sr)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans when run() returned = %d, want 1 (ready for the flush)", len(ds))
		}
		reason := ""
		for _, kv := range ds[0].Attributes() {
			if string(kv.Key) == decision.AttrDecideDiscardedReason {
				reason = kv.Value.AsString()
			}
		}
		if reason != "shutdown" {
			t.Fatalf("discarded_reason = %q, want shutdown", reason)
		}
	})
}

func decideSpans(sr *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range sr.Ended() {
		if s.Name() == decision.SpanDecide {
			out = append(out, s)
		}
	}
	return out
}
