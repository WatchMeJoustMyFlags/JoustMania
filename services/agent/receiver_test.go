package main

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/infracontext"
)

// recordingInfraLoop counts OnInfraEvaluate calls and captures the last snapshot.
type recordingInfraLoop struct {
	calls atomic.Int32
	last  infracontext.InfraContext
}

func (r *recordingInfraLoop) OnInfraEvaluate(_ context.Context, snap infracontext.InfraContext) {
	r.calls.Add(1)
	r.last = snap
}

// fixedClock returns a stable time so gating/eviction are deterministic.
func fixedClock() func() time.Time { return func() time.Time { return time.Unix(1000, 0) } }

// gameSpan appends a player_lifecycle span (game path) to td.
func gameSpan(td ptrace.Traces, serial string) {
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("player_lifecycle")
	span.Attributes().PutStr("player.serial", serial)
}

// healthSpanInto appends a controller.bluetooth_health span (infra path) to td.
func healthSpanInto(td ptrace.Traces, gapMs float64, serial, adapter string) {
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName(infracontext.SpanBluetoothHealth)
	span.Attributes().PutDouble(infracontext.AttrEventGapMs, gapMs)
	span.Attributes().PutInt(infracontext.AttrActiveControllers, 1)
	ev := span.Events().AppendEmpty()
	ev.SetName(infracontext.EventControllerSample)
	ev.Attributes().PutStr(infracontext.AttrControllerSerial, serial)
	ev.Attributes().PutDouble(infracontext.AttrMovementUpdateHz, 60)
	ev.Attributes().PutStr(infracontext.AttrControllerAdapter, adapter)
}

// testGameContext snapshots the fallback partition (game_id ""), the partition the
// label-free spans/metrics in these tests route to (zero-regression path). It
// reports !ok before any signal has created the partition.
func testGameContext(t *testing.T, mux *gamecontext.Multiplexer) (gamecontext.GameContext, bool) {
	t.Helper()
	return mux.Snapshot(gamecontext.FallbackGameID)
}

func newTestReceiver(t *testing.T) (*traceReceiver, *gamecontext.Multiplexer, *infracontext.Store, *recordingInfraLoop) {
	t.Helper()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, fixedClock())
	})
	// One Loop per game partition (#845 PR C); nil flags => disabled, no decisions.
	loops := decision.NewLoopSet(func(string) *decision.Loop {
		return decision.NewLoop(nil, logger)
	})
	infraStore := infracontext.NewStore(time.Hour, fixedClock())
	infraLoop := &recordingInfraLoop{}
	pipe := newPipeline(mux, loops, time.Hour).withInfra(infraStore, infraLoop)
	return &traceReceiver{pipe: pipe}, mux, infraStore, infraLoop
}

func TestReceiver_RoutesMixedBatchToBothStores(t *testing.T) {
	r, mux, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	gameSpan(td, "A")
	healthSpanInto(td, 42, "A", "python")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}

	// Game path: player identity enriched.
	if gc, ok := testGameContext(t, mux); !ok || gc.Players["A"] == nil {
		t.Fatal("game span must enrich the fallback game partition")
	}
	// Infra path: window + controller populated.
	infra := infraStore.Snapshot()
	if infra.Window.EventGapMs == nil || *infra.Window.EventGapMs != 42 {
		t.Fatalf("infra window not populated: %v", infra.Window.EventGapMs)
	}
	if c := infra.Controllers["A"]; c == nil || c.Adapter != "python" {
		t.Fatalf("infra controller A not populated: %+v", c)
	}
	if got := infraLoop.calls.Load(); got != 1 {
		t.Fatalf("infra loop calls = %d, want 1", got)
	}
}

func TestReceiver_GameOnlyBatchDoesNotTriggerInfra(t *testing.T) {
	r, mux, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	gameSpan(td, "A")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}
	if gc, ok := testGameContext(t, mux); !ok || gc.Players["A"] == nil {
		t.Fatal("game span must enrich the fallback game partition")
	}
	if got := infraLoop.calls.Load(); got != 0 {
		t.Fatalf("infra loop calls = %d, want 0 (no health span, no cross-contamination)", got)
	}
	if infraStore.Snapshot().Window.EventGapMs != nil {
		t.Fatal("game-only batch must not populate infra window")
	}
}

func TestReceiver_HealthOnlyBatchDoesNotCreatePlayers(t *testing.T) {
	r, mux, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	healthSpanInto(td, 7, "A", "rust")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}
	if gc, ok := testGameContext(t, mux); ok && len(gc.Players) != 0 {
		t.Fatal("health span must not create game players (no cross-contamination)")
	}
	if got := infraLoop.calls.Load(); got != 1 {
		t.Fatalf("infra loop calls = %d, want 1", got)
	}
	if infraStore.Snapshot().Controllers["A"] == nil {
		t.Fatal("health span must populate infra controller")
	}
}

func TestReceiver_SkipsOwnServiceForBothPaths(t *testing.T) {
	r, mux, infraStore, infraLoop := newTestReceiver(t)
	mux.SetOwnService("agent")
	infraStore.SetOwnService("agent")

	td := ptrace.NewTraces()
	gameSpan(td, "A")
	healthSpanInto(td, 42, "A", "python")
	// Tag every resource as the agent's own service.
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
		rss.At(i).Resource().Attributes().PutStr("service.name", "agent")
	}

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}
	if gc, ok := testGameContext(t, mux); ok && len(gc.Players) != 0 {
		t.Fatal("own-service spans must not enrich the game store")
	}
	if infraStore.Snapshot().Window.EventGapMs != nil {
		t.Fatal("own-service spans must not populate the infra store")
	}
	if got := infraLoop.calls.Load(); got != 0 {
		t.Fatalf("infra loop calls = %d, want 0 (own-service skipped)", got)
	}
}

// gameIDSpan appends a player_lifecycle span carrying a serial + game.id so the
// multiplexer routes it to that game's partition.
func gameIDSpan(td ptrace.Traces, serial, gameID string) {
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("player_lifecycle")
	span.Attributes().PutStr("player.serial", serial)
	span.Attributes().PutStr("game.id", gameID)
}

// metricGauge appends a single-datapoint gauge to md with the given label set,
// mirroring the producers' OTLP metric shape. Uses the wire label names directly
// (game_id / serial / metric names) since the extractor constants are unexported.
func metricGauge(md pmetric.Metrics, name string, value float64, labels map[string]string) {
	m := md.ResourceMetrics().AppendEmpty().ScopeMetrics().AppendEmpty().Metrics().AppendEmpty()
	m.SetName(name)
	dp := m.SetEmptyGauge().DataPoints().AppendEmpty()
	dp.SetDoubleValue(value)
	for k, v := range labels {
		dp.Attributes().PutStr(k, v)
	}
}

// liveGameMetrics builds a metric batch that makes a game gate-eligible: a
// game_active=1 datapoint plus a game_player_alive=1 datapoint for one player,
// both tagged with game_id so the multiplexer routes them to that game.
func liveGameMetrics(gameID, serial string) pmetric.Metrics {
	md := pmetric.NewMetrics()
	metricGauge(md, "game_active", 1, map[string]string{"game_id": gameID})
	metricGauge(md, "game_player_alive", 1, map[string]string{"game_id": gameID, "serial": serial})
	return md
}

// TestReceiver_PerGameBudgetIndependence is the integration-level #845 AC: two
// concurrent games, driven through the full OTLP metrics Export path, each get
// their own decision Loop (own budget). Game A is driven until its per-minute
// budget is exhausted (its decisions block with rate_limit); game B — sharing the
// same LoopSet through the same receiver — still dispatches unblocked.
func TestReceiver_PerGameBudgetIndependence(t *testing.T) {
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	clock := time.Unix(5000, 0)
	mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
		return gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	})

	// Per-game loop: budget of 2 (grant_shield costs 1), recording tracer per game.
	recorders := map[string]*tracetest.SpanRecorder{}
	loops := decision.NewLoopSet(func(gameID string) *decision.Loop {
		sr := tracetest.NewSpanRecorder()
		tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
		fl := flags.Snapshot{
			Enabled:              true,
			Mode:                 "rules",
			InterventionsAllowed: []string{"grant_shield"},
			Policy:               flags.Policy{MaxInterventionsPerMinute: 2},
		}
		l := decision.NewLoop(staticFlags{fl}, logger)
		l.Rules = staticRules{out: []decision.Decision{{Intervention: "grant_shield"}}}
		l.Tracer = tp.Tracer("test")
		recorders[gameID] = sr
		return l
	})

	pipe := newPipeline(mux, loops, time.Hour)
	pipe.now = func() time.Time { return clock }
	r := &metricsReceiver{pipe: pipe}

	// Drive game A four times: cycles 1-2 dispatch (budget 2), cycles 3-4 block.
	for i := 0; i < 4; i++ {
		if _, err := r.Export(context.Background(), pmetricotlp.NewExportRequestFromMetrics(liveGameMetrics("game-A", "PA"))); err != nil {
			t.Fatalf("game-A export %d: %v", i, err)
		}
	}
	// Drive game B once: its budget is untouched, so it must dispatch.
	if _, err := r.Export(context.Background(), pmetricotlp.NewExportRequestFromMetrics(liveGameMetrics("game-B", "PB"))); err != nil {
		t.Fatalf("game-B export: %v", err)
	}

	// Game A: among its action spans, at least one is blocked (budget exhausted).
	aBlocked := countBlockedActions(recorders["game-A"].Ended())
	if aBlocked == 0 {
		t.Errorf("game-A: no blocked actions; its budget was never exhausted (budget not per-game?)")
	}
	// Game B: its single action span dispatched (not blocked).
	bBlocked := countBlockedActions(recorders["game-B"].Ended())
	bActions := spansByNameRO(recorders["game-B"].Ended(), decision.SpanAction)
	if len(bActions) == 0 || bBlocked != 0 {
		t.Errorf("game-B: actions=%d blocked=%d, want >=1 dispatched and 0 blocked (B's budget is independent of A)", len(bActions), bBlocked)
	}
}

// staticFlags / staticRules are minimal local fakes for the receiver integration
// test (the decision package's own fakes are not exported).
type staticFlags struct{ snap flags.Snapshot }

func (f staticFlags) Evaluate(context.Context) flags.Snapshot { return f.snap }

type staticRules struct{ out []decision.Decision }

func (r staticRules) Evaluate(context.Context, gamecontext.GameContext) []decision.Decision {
	return r.out
}

func spansByNameRO(spans []sdktrace.ReadOnlySpan, name string) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == name {
			out = append(out, s)
		}
	}
	return out
}

// countBlockedActions counts agent.action spans carrying decision.blocked=true.
func countBlockedActions(spans []sdktrace.ReadOnlySpan) int {
	n := 0
	for _, s := range spansByNameRO(spans, decision.SpanAction) {
		for _, kv := range s.Attributes() {
			if string(kv.Key) == decision.AttrDecisionBlocked && kv.Value.AsBool() {
				n++
			}
		}
	}
	return n
}

// TestReceiver_PartitionsByGameID: a single trace batch carrying spans for two
// distinct game_ids lands in two disjoint partitions through the receiver path
// (#845 PR B) — no cross-game player bleed.
func TestReceiver_PartitionsByGameID(t *testing.T) {
	r, mux, _, _ := newTestReceiver(t)

	td := ptrace.NewTraces()
	gameIDSpan(td, "P1", "game-A")
	gameIDSpan(td, "P2", "game-B")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}

	a, okA := mux.Snapshot("game-A")
	b, okB := mux.Snapshot("game-B")
	if !okA || !okB {
		t.Fatalf("both partitions must exist: okA=%v okB=%v", okA, okB)
	}
	if _, ok := a.Players["P1"]; !ok {
		t.Error("P1 should be in game-A")
	}
	if _, ok := a.Players["P2"]; ok {
		t.Error("P2 bled into game-A")
	}
	if _, ok := b.Players["P2"]; !ok {
		t.Error("P2 should be in game-B")
	}
}
