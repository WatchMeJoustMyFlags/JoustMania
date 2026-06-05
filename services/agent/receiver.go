package main

import (
	"context"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric/pmetricotlp"
	"go.opentelemetry.io/collector/pdata/ptrace/ptraceotlp"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gate"
)

// Full OTLP gRPC service names, recorded as rpc.service (semconv) on the
// agent.span_received root span.
const (
	otlpTraceService   = "opentelemetry.proto.collector.trace.v1.TraceService"
	otlpMetricsService = "opentelemetry.proto.collector.metrics.v1.MetricsService"
)

// pipeline ties the context store and decision loop together. On each signal
// update it snapshots the store and, if the gate allows, runs the loop.
type pipeline struct {
	store     *gamecontext.Store
	loop      *decision.Loop
	playerTTL time.Duration
	now       func() time.Time
}

func newPipeline(store *gamecontext.Store, loop *decision.Loop, playerTTL time.Duration) *pipeline {
	return &pipeline{
		store:     store,
		loop:      loop,
		playerTTL: playerTTL,
		now:       time.Now,
	}
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

// Export ingests a batch of traces.
func (r *traceReceiver) Export(ctx context.Context, req ptraceotlp.ExportRequest) (ptraceotlp.ExportResponse, error) {
	t0 := time.Now()
	if r.pipe.store.ApplySpans(req.Traces()) {
		r.pipe.signalUpdated(ctx, decision.EvalTrigger{
			Signal:     "traces",
			RPCService: otlpTraceService,
			T0:         t0,
		})
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
