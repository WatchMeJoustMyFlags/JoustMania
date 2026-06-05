package main

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/ptrace"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"

	"github.com/joustmania/agent/decision"
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

func newTestReceiver(t *testing.T) (*traceReceiver, *gamecontext.Store, *infracontext.Store, *recordingInfraLoop) {
	t.Helper()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	store := gamecontext.NewStore(time.Hour, time.Hour, fixedClock())
	loop := decision.NewLoop(nil, logger) // nil flags => disabled, no decisions
	infraStore := infracontext.NewStore(time.Hour, fixedClock())
	infraLoop := &recordingInfraLoop{}
	pipe := newPipeline(store, loop, time.Hour).withInfra(infraStore, infraLoop)
	return &traceReceiver{pipe: pipe}, store, infraStore, infraLoop
}

func TestReceiver_RoutesMixedBatchToBothStores(t *testing.T) {
	r, store, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	gameSpan(td, "A")
	healthSpanInto(td, 42, "A", "python")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}

	// Game path: player identity enriched.
	if store.Snapshot().Players["A"] == nil {
		t.Fatal("game span must enrich the game store")
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
	r, store, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	gameSpan(td, "A")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}
	if store.Snapshot().Players["A"] == nil {
		t.Fatal("game span must enrich the game store")
	}
	if got := infraLoop.calls.Load(); got != 0 {
		t.Fatalf("infra loop calls = %d, want 0 (no health span, no cross-contamination)", got)
	}
	if infraStore.Snapshot().Window.EventGapMs != nil {
		t.Fatal("game-only batch must not populate infra window")
	}
}

func TestReceiver_HealthOnlyBatchDoesNotCreatePlayers(t *testing.T) {
	r, store, infraStore, infraLoop := newTestReceiver(t)

	td := ptrace.NewTraces()
	healthSpanInto(td, 7, "A", "rust")

	if _, err := r.Export(context.Background(), ptraceotlp.NewExportRequestFromTraces(td)); err != nil {
		t.Fatalf("Export error: %v", err)
	}
	if len(store.Snapshot().Players) != 0 {
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
	r, store, infraStore, infraLoop := newTestReceiver(t)
	store.SetOwnService("agent")
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
	if len(store.Snapshot().Players) != 0 {
		t.Fatal("own-service spans must not enrich the game store")
	}
	if infraStore.Snapshot().Window.EventGapMs != nil {
		t.Fatal("own-service spans must not populate the infra store")
	}
	if got := infraLoop.calls.Load(); got != 0 {
		t.Fatalf("infra loop calls = %d, want 0 (own-service skipped)", got)
	}
}
