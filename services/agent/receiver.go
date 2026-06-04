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

// signalUpdated is called after any received signal mutates the store.
func (p *pipeline) signalUpdated() {
	now := p.now
	if now == nil {
		now = time.Now
	}
	snap := p.store.Snapshot()
	if gate.ShouldEvaluate(snap, now(), p.playerTTL) {
		p.loop.OnEvaluate(snap)
	}
}

// traceReceiver implements the OTLP trace gRPC service.
type traceReceiver struct {
	ptraceotlp.UnimplementedGRPCServer
	pipe *pipeline
}

// Export ingests a batch of traces.
func (r *traceReceiver) Export(_ context.Context, req ptraceotlp.ExportRequest) (ptraceotlp.ExportResponse, error) {
	if r.pipe.store.ApplySpans(req.Traces()) {
		r.pipe.signalUpdated()
	}
	return ptraceotlp.NewExportResponse(), nil
}

// metricsReceiver implements the OTLP metrics gRPC service.
type metricsReceiver struct {
	pmetricotlp.UnimplementedGRPCServer
	pipe *pipeline
}

// Export ingests a batch of metrics.
func (r *metricsReceiver) Export(_ context.Context, req pmetricotlp.ExportRequest) (pmetricotlp.ExportResponse, error) {
	if r.pipe.store.ApplyMetrics(req.Metrics()) {
		r.pipe.signalUpdated()
	}
	return pmetricotlp.NewExportResponse(), nil
}
