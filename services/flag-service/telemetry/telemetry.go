// Package telemetry holds the flag service's OTel instruments and span helpers
// (telemetry.md §10). Labels never carry game ids, serials or the free-text
// writer label (unbounded cardinality); those live on the spans.
package telemetry

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/flag-service/state"
)

// Name is the instrumentation scope of the service's tracer and meter.
const Name = "github.com/joustmania/flag-service"

// Span names (telemetry.md §10.1).
const (
	SpanWrite        = "flagservice.write"
	SpanGameRegister = "flagservice.game.register"
	SpanGameEnd      = "flagservice.game.end"
	SpanSyncPush     = "flagservice.sync.push"
	SpanBaselineLoad = "flagservice.baseline.load"
	OpSet            = "set"
	OpAppend         = "append"
	OpClear          = "clear"
	OpDryRun         = "dry_run"
	ResultOK         = "ok"
	ResultRejected   = "rejected"
	ResultError      = "error"
	RejectUnknownSel = "unknown_selector"
	RejectUnknownSet = "unknown_flag_set"
)

// Metrics are the flag service's instruments (telemetry.md §10.2).
type Metrics struct {
	Writes             metric.Int64Counter
	WriteDuration      metric.Float64Histogram
	Propagation        metric.Float64Histogram
	ScopesExpired      metric.Int64Counter
	ExperimentsExpired metric.Int64Counter
	SyncRejected       metric.Int64Counter
	AdminStateCorrupt  metric.Int64Counter
	registeredGaugeCbs metric.Registration
}

// NewMetrics creates the instruments on meter. stats feeds the gauges.
func NewMetrics(meter metric.Meter, stats func() state.Stats) (*Metrics, error) {
	m := &Metrics{}
	var err error
	if m.Writes, err = meter.Int64Counter("flagservice_writes_total",
		metric.WithDescription("Flag-service writes by flag set, flag, op, result and reason (UC-053 BR-005)")); err != nil {
		return nil, err
	}
	if m.WriteDuration, err = meter.Float64Histogram("flagservice_write_duration_seconds",
		metric.WithDescription("Validate + render + acknowledge one write (NFR-053)"),
		metric.WithExplicitBucketBoundaries(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5)); err != nil {
		return nil, err
	}
	if m.Propagation, err = meter.Float64Histogram("flagservice_propagation_seconds",
		metric.WithDescription("Write accepted -> merged configuration handed to an open sync stream (NFR-050)"),
		metric.WithExplicitBucketBoundaries(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1)); err != nil {
		return nil, err
	}
	if m.ScopesExpired, err = meter.Int64Counter("flagservice_game_scopes_expired_total",
		metric.WithDescription("Game scopes dropped by expiry (UC-053 A11)")); err != nil {
		return nil, err
	}
	if m.ExperimentsExpired, err = meter.Int64Counter("flagservice_experiments_expired_total",
		metric.WithDescription("Experiments whose arm values were dropped because no game referenced them (NFR-051)")); err != nil {
		return nil, err
	}
	if m.SyncRejected, err = meter.Int64Counter("flagservice_sync_rejected_total",
		metric.WithDescription("Rejected SyncFlags / FetchAllFlags calls by reason")); err != nil {
		return nil, err
	}
	if m.AdminStateCorrupt, err = meter.Int64Counter("flagservice_admin_state_corrupt_total",
		metric.WithDescription("Corrupt state files moved aside at start")); err != nil {
		return nil, err
	}
	liveGames, err := meter.Int64ObservableGauge("flagservice_live_games",
		metric.WithDescription("Registered game scopes by game kind"))
	if err != nil {
		return nil, err
	}
	scopeValues, err := meter.Int64ObservableGauge("flagservice_scope_values",
		metric.WithDescription("Game-scoped and experiment values held, by flag set (NFR-051)"))
	if err != nil {
		return nil, err
	}
	snapshotBytes, err := meter.Int64ObservableGauge("flagservice_snapshot_bytes",
		metric.WithDescription("Size of the merged configuration (NFR-051)"))
	if err != nil {
		return nil, err
	}
	streams, err := meter.Int64ObservableGauge("flagservice_sync_streams",
		metric.WithDescription("Open SyncFlags streams; 0 = flagd not connected, writes return 503 no_sync_stream"))
	if err != nil {
		return nil, err
	}
	overlays, err := meter.Int64ObservableGauge("flagservice_overlays",
		metric.WithDescription("Stack-wide overlays in effect, by flag set and persisted"))
	if err != nil {
		return nil, err
	}
	persistErrors, err := meter.Int64ObservableCounter("flagservice_admin_persist_errors_total",
		metric.WithDescription("Failed writes of the admin state file (rolled back and refused, except a kill switch, which applies in memory)"))
	if err != nil {
		return nil, err
	}
	m.registeredGaugeCbs, err = meter.RegisterCallback(func(_ context.Context, o metric.Observer) error {
		st := stats()
		for kind, n := range st.LiveGamesByKind {
			o.ObserveInt64(liveGames, int64(n), metric.WithAttributes(attribute.String("game_kind", kind)))
		}
		if len(st.LiveGamesByKind) == 0 {
			o.ObserveInt64(liveGames, 0)
		}
		for set, n := range st.ScopeValuesBySet {
			o.ObserveInt64(scopeValues, int64(n), metric.WithAttributes(attribute.String("flag_set", set)))
		}
		for set, n := range st.OverlaysBySet {
			o.ObserveInt64(overlays, int64(n), metric.WithAttributes(attribute.String("flag_set", set), attribute.Bool("persisted", false)))
		}
		for set, n := range st.PersistedOverlaysBySet {
			o.ObserveInt64(overlays, int64(n), metric.WithAttributes(attribute.String("flag_set", set), attribute.Bool("persisted", true)))
		}
		o.ObserveInt64(snapshotBytes, int64(st.SnapshotBytes))
		o.ObserveInt64(streams, int64(st.SyncStreams))
		o.ObserveInt64(persistErrors, st.PersistErrors)
		return nil
	}, liveGames, scopeValues, snapshotBytes, streams, overlays, persistErrors)
	if err != nil {
		return nil, err
	}
	return m, nil
}

// WriteAttrs are the flagservice.write span attributes of one write.
func WriteAttrs(op string, req state.WriteRequest, res state.WriteResult, result, reason string) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		attribute.String("feature_flag.key", req.Flag),
		attribute.String("feature_flag.set.id", req.Set),
		attribute.String("flagservice.op", op),
		attribute.String("flagservice.result", result),
		attribute.String("flagservice.reason", reason),
		attribute.String("flagservice.epoch", res.Epoch),
		attribute.Int64("flagservice.snapshot_version", int64(res.SnapshotVersion)),
		attribute.Bool("flagservice.persisted", res.Persisted),
	}
	add := func(key, v string) {
		if v != "" {
			attrs = append(attrs, attribute.String(key, v))
		}
	}
	add("feature_flag.result.variant", res.Variant)
	if len(res.Value) > 0 {
		attrs = append(attrs, attribute.String("feature_flag.result.value", string(res.Value)))
	}
	add("flagservice.kind", res.Kind)
	add("flagservice.scope", res.ScopeKey)
	add("flagservice.writer", req.Writer)
	add("game.id", req.Scope.GameID)
	add("game.kind", res.GameKind)
	add("player.serial", req.Scope.Serial)
	add("experiment.id", req.Scope.ExperimentID)
	add("experiment.arm", req.Scope.Arm)
	add("decision.intervention_id", req.DecisionID)
	if res.Seq > 0 {
		attrs = append(attrs, attribute.Int64("flagservice.seq", res.Seq))
	}
	return attrs
}

// RecordWrite counts one write (flagservice_writes_total).
func (m *Metrics) RecordWrite(ctx context.Context, set, flag, op, result, reason string) {
	if m == nil {
		return
	}
	m.Writes.Add(ctx, 1, metric.WithAttributes(
		attribute.String("flag_set", set),
		attribute.String("flag", flag),
		attribute.String("op", op),
		attribute.String("result", result),
		attribute.String("reason", reason),
	))
}

// GameEndSpan records a flagservice.game.end span for a dropped scope.
func GameEndSpan(ctx context.Context, tracer trace.Tracer, end state.GameEnd) {
	_, span := tracer.Start(ctx, SpanGameEnd, trace.WithAttributes(
		attribute.String("game.id", end.GameID),
		attribute.String("flagservice.reason", end.Reason),
		attribute.Int("flagservice.dropped_values", end.DroppedValues),
		attribute.String("flagservice.coordinator_instance", end.CoordinatorInstance),
	))
	span.End()
}
