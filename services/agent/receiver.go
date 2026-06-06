package main

import (
	"context"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gate"
	"github.com/joustmania/agent/infracontext"
)

// Full OTLP gRPC service names, recorded as rpc.service (semconv) on the
// agent.span_received root span.
const (
	otlpTraceService   = "opentelemetry.proto.collector.trace.v1.TraceService"
	otlpMetricsService = "opentelemetry.proto.collector.metrics.v1.MetricsService"
)

// pipeline ties the context stores and decision loops together. On each signal
// update it snapshots the relevant partition(s) and, if the gate allows, runs the
// per-game loop for that partition.
//
// There are two parallel observe paths sharing the one OTLP trace receiver:
//   - the game path (mux + loops): the GameContext multiplexer (#845 PR B) — one
//     Store partition per game_id, fallback partition "" for unlabeled signals —
//     plus the per-game LoopSet (#845 PR C) — one decision.Loop per partition.
//   - the infrastructure path (infraStore + infraLoop): the #733 Bluetooth-health
//     observe path. infraLoop may be nil, in which case the infra path is inert.
//
// PER-GAME BUDGET (#845 PR C): each partition gets its OWN decision.Loop via the
// LoopSet, lazily created on first evaluation. Each Loop owns its weighted rate
// limiter (per-minute budget), its log/span throttle slot, and its per-cycle
// LayerState — so interventions from one game no longer draw down another game's
// budget, and one game's throttle no longer suppresses another's spans/logs. In
// single-game mode every signal lands on the fallback partition's single loop, so
// behavior collapses to exactly the single-store agent's.
type pipeline struct {
	mux       *gamecontext.Multiplexer
	loops     *decision.LoopSet
	playerTTL time.Duration

	infraStore *infracontext.Store
	infraLoop  decision.InfraEvaluator

	now func() time.Time
}

func newPipeline(mux *gamecontext.Multiplexer, loops *decision.LoopSet, playerTTL time.Duration) *pipeline {
	return &pipeline{
		mux:       mux,
		loops:     loops,
		playerTTL: playerTTL,
		now:       time.Now,
	}
}

// withInfra attaches the infrastructure observe path. Returns the pipeline for
// chaining at construction.
func (p *pipeline) withInfra(infraStore *infracontext.Store, infraLoop decision.InfraEvaluator) *pipeline {
	p.infraStore = infraStore
	p.infraLoop = infraLoop
	return p
}

// infraUpdated is called after the Bluetooth-health context mutates. It snapshots
// the infra store and triggers the (stub) infrastructure evaluation loop. Parallel
// to signalUpdated for the game path; no gate yet (the real gate lands in PR E).
func (p *pipeline) infraUpdated(ctx context.Context) {
	if p.infraStore == nil || p.infraLoop == nil {
		return
	}
	p.infraLoop.OnInfraEvaluate(ctx, p.infraStore.Snapshot())
}

// signalUpdated is called after a received signal mutates one or more partitions.
// gameIDs is the deduped set of partitions an Apply pass touched. For each, the
// partition is snapshotted and, if the gate allows, evaluated through THAT game's
// own Loop (per-game budget/throttle, #845 PR C). The caller's gRPC context and
// EvalTrigger flow through so the decision loop can emit its audit trace (issue
// #724) with accurate timing and rpc.* attributes.
func (p *pipeline) signalUpdated(ctx context.Context, gameIDs []string, trig decision.EvalTrigger) {
	now := p.now
	if now == nil {
		now = time.Now
	}
	t := now()
	for _, gameID := range gameIDs {
		// A partition can be evicted between Apply and here; Snapshot reports !ok and
		// we simply skip it (a resumed signal recreates the partition next batch).
		snap, ok := p.mux.Snapshot(gameID)
		if !ok {
			continue
		}
		if gate.ShouldEvaluate(snap, t, p.playerTTL) {
			// loops.For lazily creates this game's Loop on first touch, so its budget
			// and throttle state are isolated from every other game's.
			p.loops.For(gameID).OnEvaluate(ctx, snap, trig)
		}
	}
}

// traceReceiver implements the OTLP trace gRPC service.
type traceReceiver struct {
	ptraceotlp.UnimplementedGRPCServer
	pipe *pipeline
}

// Export ingests a batch of traces. A single batch may carry both game spans
// (player_lifecycle etc.) and controller.bluetooth_health spans; each store sees
// the whole batch and recognizes only its own span kinds, so both observe paths
// fire independently with no cross-contamination. Both stores honor the same
// own-service self-ingestion skip internally.
func (r *traceReceiver) Export(ctx context.Context, req ptraceotlp.ExportRequest) (ptraceotlp.ExportResponse, error) {
	t0 := time.Now()
	td := req.Traces()
	if ids := r.pipe.mux.ApplySpans(td); len(ids) > 0 {
		r.pipe.signalUpdated(ctx, ids, decision.EvalTrigger{
			Signal:     "traces",
			RPCService: otlpTraceService,
			T0:         t0,
		})
	}
	if r.pipe.infraStore != nil && r.pipe.infraStore.ApplySpans(td) {
		r.pipe.infraUpdated(ctx)
	}
	return ptraceotlp.NewExportResponse(), nil
}

// metricsReceiver implements the OTLP metrics gRPC service.
type metricsReceiver struct {
	pmetricotlp.UnimplementedGRPCServer
	pipe *pipeline
}

// Export ingests a batch of metrics.
func (r *metricsReceiver) Export(ctx context.Context, req pmetricotlp.ExportRequest) (pmetricotlp.ExportResponse, error) {
	t0 := time.Now()
	if ids := r.pipe.mux.ApplyMetrics(req.Metrics()); len(ids) > 0 {
		r.pipe.signalUpdated(ctx, ids, decision.EvalTrigger{
			Signal:     "metrics",
			RPCService: otlpMetricsService,
			T0:         t0,
		})
	}
	return pmetricotlp.NewExportResponse(), nil
}
