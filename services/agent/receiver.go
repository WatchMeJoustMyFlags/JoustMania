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
// update it snapshots the relevant store and, if the gate allows, runs the loop.
//
// There are two parallel observe paths sharing the one OTLP trace receiver:
//   - the game path (store + loop): the existing GameContext + decision loop.
//   - the infrastructure path (infraStore + infraLoop): the #733 Bluetooth-health
//     observe path. infraLoop may be nil, in which case the infra path is inert.
type pipeline struct {
	store     *gamecontext.Store
	loop      *decision.Loop
	playerTTL time.Duration

	infraStore *infracontext.Store
	infraLoop  decision.InfraEvaluator

	now func() time.Time
}

func newPipeline(store *gamecontext.Store, loop *decision.Loop, playerTTL time.Duration) *pipeline {
	return &pipeline{
		store:     store,
		loop:      loop,
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

// signalUpdated is called after any received signal mutates the store. The
// caller's gRPC context and EvalTrigger flow through so the decision loop can
// emit its audit trace (issue #724) with accurate timing and rpc.* attributes.
func (p *pipeline) signalUpdated(ctx context.Context, trig decision.EvalTrigger) {
	now := p.now
	if now == nil {
		now = time.Now
	}
	snap := p.store.Snapshot()
	if gate.ShouldEvaluate(snap, now(), p.playerTTL) {
		p.loop.OnEvaluate(ctx, snap, trig)
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
	if r.pipe.store.ApplySpans(td) {
		r.pipe.signalUpdated(ctx, decision.EvalTrigger{
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
	if r.pipe.store.ApplyMetrics(req.Metrics()) {
		r.pipe.signalUpdated(ctx, decision.EvalTrigger{
			Signal:     "metrics",
			RPCService: otlpMetricsService,
			T0:         t0,
		})
	}
	return pmetricotlp.NewExportResponse(), nil
}
