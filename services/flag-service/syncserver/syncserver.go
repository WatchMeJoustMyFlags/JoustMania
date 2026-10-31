// Package syncserver implements flagd.sync.v1.FlagSyncService for flagd
// (FR-131, services.md §7.1): one SyncFlags stream carries the merged
// configuration of every served set, each flag tagged with its
// metadata.flagSetId; a flagSetId=<set> selector (flagd-selector header or
// body) narrows it to one set.
//
// UC-053 BR-007: the flag service is a sync source only. It evaluates no flag;
// every OpenFeature client keeps resolving through stock flagd.
package syncserver

import (
	"context"
	"strings"
	"time"

	syncv1grpc "buf.build/gen/go/open-feature/flagd/grpc/go/flagd/sync/v1/syncv1grpc"
	syncv1 "buf.build/gen/go/open-feature/flagd/protocolbuffers/go/flagd/sync/v1"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/telemetry"
)

// SelectorHeader is the metadata key flagd and the in-process providers use
// for the selector (matched case-insensitively: gRPC lowercases keys).
const SelectorHeader = "flagd-selector"

// Keepalive (services.md §7.1): flagd v0.16.0's sync client sets none, so the
// server pings to detect a dead flagd, and allows client pings from the
// in-process SDK clients (they ping every 30 s).
var (
	KeepaliveParams = keepalive.ServerParameters{Time: 30 * time.Second, Timeout: 10 * time.Second}
	KeepalivePolicy = keepalive.EnforcementPolicy{MinTime: 10 * time.Second, PermitWithoutStream: true}
)

// ServerOptions returns the gRPC server options the sync server needs.
func ServerOptions() []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.KeepaliveParams(KeepaliveParams),
		grpc.KeepaliveEnforcementPolicy(KeepalivePolicy),
	}
}

// Server is the FlagSyncService implementation.
type Server struct {
	syncv1grpc.UnimplementedFlagSyncServiceServer
	State   *state.State
	Metrics *telemetry.Metrics
	Tracer  trace.Tracer
	Links   *telemetry.Links
	Now     func() time.Time
}

// Register registers the service on a gRPC server.
func (s *Server) Register(g *grpc.Server) { syncv1grpc.RegisterFlagSyncServiceServer(g, s) }

// parseSelector returns the flag set a selector names ("" = every served set).
// Only the flagSetId key is supported (flagd selector syntax); anything else is
// an unknown selector.
func parseSelector(raw string) (string, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", true
	}
	key, val, ok := strings.Cut(raw, "=")
	if !ok || strings.TrimSpace(key) != "flagSetId" || strings.TrimSpace(val) == "" {
		return "", false
	}
	return strings.TrimSpace(val), true
}

func (s *Server) selector(ctx context.Context, body string) (string, error) {
	// body is the deprecated request field (SA1019): kept as the fallback for
	// flagd versions that send no flagd-selector header, which wins.
	raw := body
	if md, ok := metadata.FromIncomingContext(ctx); ok {
		if vals := md.Get(SelectorHeader); len(vals) > 0 && vals[0] != "" {
			raw = vals[0]
		}
	}
	set, ok := parseSelector(raw)
	if !ok {
		s.reject(ctx, telemetry.RejectUnknownSel)
		return "", status.Errorf(codes.InvalidArgument, "unknown selector %q: only flagSetId=<set> is supported", raw)
	}
	if set != "" && !s.State.Served(set) {
		s.reject(ctx, telemetry.RejectUnknownSet)
		return "", status.Errorf(codes.InvalidArgument, "flag set %q is not served by the flag service", set)
	}
	return set, nil
}

func (s *Server) reject(ctx context.Context, reason string) {
	if s.Metrics != nil {
		s.Metrics.SyncRejected.Add(ctx, 1, metric.WithAttributes(attribute.String("reason", reason)))
	}
}

func (s *Server) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

// SyncFlags streams the merged configuration: once at the start, then after
// every change. A slow stream skips intermediate versions (latest wins).
func (s *Server) SyncFlags(req *syncv1.SyncFlagsRequest, stream grpc.ServerStreamingServer[syncv1.SyncFlagsResponse]) error {
	ctx := stream.Context()
	set, err := s.selector(ctx, req.GetSelector()) //nolint:staticcheck // SA1019: body fallback of the selector header
	if err != nil {
		return err
	}
	sub, err := s.State.Subscribe(set)
	if err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	defer sub.Close()
	var sent uint64
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-sub.C:
		}
		payload, version, err := s.State.Snapshot(set)
		if err != nil {
			return status.Error(codes.Internal, err.Error())
		}
		if version == sent {
			continue
		}
		if err := s.send(ctx, stream, payload, version, sent == 0); err != nil {
			return err
		}
		sent = version
	}
}

func (s *Server) send(ctx context.Context, stream grpc.ServerStreamingServer[syncv1.SyncFlagsResponse], payload []byte, version uint64, first bool) error {
	var opts []trace.SpanStartOption
	entry, linked := s.Links.Lookup(version)
	if linked && entry.SC.IsValid() {
		opts = append(opts, trace.WithLinks(trace.Link{SpanContext: entry.SC}))
	}
	opts = append(opts, trace.WithAttributes(
		attribute.Int64("flagservice.snapshot_version", int64(version)),
		attribute.Int("flagservice.snapshot_bytes", len(payload)),
		attribute.Int("flagservice.streams", s.State.SyncStreams()),
		attribute.String("flagservice.epoch", s.State.Epoch()),
	))
	_, span := s.Tracer.Start(ctx, telemetry.SpanSyncPush, opts...)
	defer span.End()
	resp := &syncv1.SyncFlagsResponse{}
	resp.SetFlagConfiguration(string(payload))
	if err := stream.Send(resp); err != nil {
		span.RecordError(err)
		return err
	}
	if !first && linked && s.Metrics != nil {
		s.Metrics.Propagation.Record(ctx, s.now().Sub(entry.At).Seconds())
	}
	return nil
}

// FetchAllFlags returns the current merged configuration, filtered like
// SyncFlags.
func (s *Server) FetchAllFlags(ctx context.Context, req *syncv1.FetchAllFlagsRequest) (*syncv1.FetchAllFlagsResponse, error) {
	set, err := s.selector(ctx, req.GetSelector()) //nolint:staticcheck // SA1019: body fallback of the selector header
	if err != nil {
		return nil, err
	}
	payload, _, err := s.State.Snapshot(set)
	if err != nil {
		return nil, status.Error(codes.Internal, err.Error())
	}
	resp := &syncv1.FetchAllFlagsResponse{}
	resp.SetFlagConfiguration(string(payload))
	return resp, nil
}
