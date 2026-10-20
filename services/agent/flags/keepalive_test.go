package flags

import (
	"context"
	"net"
	"testing"
	"time"

	"buf.build/gen/go/open-feature/flagd/grpc/go/flagd/sync/v1/syncv1grpc"
	syncv1 "buf.build/gen/go/open-feature/flagd/protocolbuffers/go/flagd/sync/v1"
	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
	"google.golang.org/grpc"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
)

// UC: UC-045 A3 Pre-1
// The sync connection's keepalive stays within flagd's ping policy (#1768): flagd
// v0.16.0's sync server enforces grpc-go's default (one ping per 5 min, none without a
// stream), and a client pinging faster gets GOAWAY too_many_pings, which drops the
// stream and makes the agent fail closed (UC-045 A3) while flagd is healthy.
func TestSyncKeepalive_WithinFlagdPingPolicy(t *testing.T) {
	t.Run("UC-045/A3 UC-045/Pre-1 keepalive ping interval at or above flagd's MinTime", func(t *testing.T) {
		if syncKeepaliveParams.Time < flagdServerMinPingInterval {
			t.Fatalf("keepalive Time = %s, want >= %s (flagd's enforced ping interval)",
				syncKeepaliveParams.Time, flagdServerMinPingInterval)
		}
	})
	t.Run("UC-045/A3 no pings without an open stream", func(t *testing.T) {
		if syncKeepaliveParams.PermitWithoutStream {
			t.Fatal("PermitWithoutStream = true: flagd counts a ping without a stream as a strike")
		}
	})
	t.Run("UC-045/A3 keepalive still detects a dead connection", func(t *testing.T) {
		if syncKeepaliveParams.Time <= 0 || syncKeepaliveParams.Timeout <= 0 {
			t.Fatalf("keepalive disabled: %+v", syncKeepaliveParams)
		}
	})
}

// UC: UC-045 A3 Pre-1
// Every agent provider is built with the keepalive dial-option override (#1768): without
// it v0.6.0 dials with its own 30 s keepalive and flagd drops the stream.
func TestFlagdProviderOptions_CarryKeepaliveOverride(t *testing.T) {
	t.Run("UC-045/A3 provider options carry the sync dial-option override", func(t *testing.T) {
		for _, set := range []string{Domain, RolloutDomain} {
			cfg, err := flagd.NewProviderConfiguration(
				flagdProviderOptions(ProviderConfig{Host: "flagd", Port: 8015}, set, time.Second))
			if err != nil {
				t.Fatalf("%s: NewProviderConfiguration: %v", set, err)
			}
			if got, want := len(cfg.GrpcDialOptionsOverride), len(syncDialOptions()); got == 0 || got != want {
				t.Fatalf("%s: GrpcDialOptionsOverride has %d options, want the %d of syncDialOptions",
					set, got, want)
			}
		}
	})
}

// fakeSyncServer is a gRPC flagd sync service that records the selector header and
// sends one flag configuration per stream, then holds the stream open.
type fakeSyncServer struct {
	syncv1grpc.UnimplementedFlagSyncServiceServer
	config    string
	selectors chan string
}

func (s *fakeSyncServer) SyncFlags(_ *syncv1.SyncFlagsRequest, stream grpc.ServerStreamingServer[syncv1.SyncFlagsResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	selector := ""
	if v := md.Get("flagd-selector"); len(v) > 0 {
		selector = v[0]
	}
	select {
	case s.selectors <- selector:
	default:
	}
	if err := stream.Send(&syncv1.SyncFlagsResponse{FlagConfiguration: s.config}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return nil
}

// UC: UC-045 A3 Pre-1
// The dial-option override (#1768) replaces all of v0.6.0's own dial options: the real
// flagdFactory must still reach a plaintext sync server, send its flagSetId selector and
// serve the synced flags. The server enforces flagd's ping policy explicitly.
func TestFlagdFactory_SyncsOverKeepaliveDialOptions(t *testing.T) {
	t.Run("UC-045/A3 plaintext sync with selector over the override dial options", func(t *testing.T) {
		lis, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatalf("listen: %v", err)
		}
		srv := grpc.NewServer(grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
			MinTime:             flagdServerMinPingInterval,
			PermitWithoutStream: false,
		}))
		fake := &fakeSyncServer{config: newFakeFlagdSource("on").config("on"), selectors: make(chan string, 4)}
		syncv1grpc.RegisterFlagSyncServiceServer(srv, fake)
		go func() { _ = srv.Serve(lis) }()
		t.Cleanup(srv.Stop)

		port := uint16(lis.Addr().(*net.TCPAddr).Port)
		inner, err := flagdFactory(ProviderConfig{Host: "127.0.0.1", Port: port}, Domain)(5 * time.Second)
		if err != nil {
			t.Fatalf("flagdFactory: %v", err)
		}
		t.Cleanup(inner.Shutdown)
		if err := inner.Init(openfeature.EvaluationContext{}); err != nil {
			t.Fatalf("Init (first sync over the override dial options): %v", err)
		}
		select {
		case got := <-fake.selectors:
			if want := "flagSetId=" + Domain; got != want {
				t.Errorf("selector = %q, want %q", got, want)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("sync server never saw a SyncFlags call")
		}
		res := inner.BooleanEvaluation(context.Background(), keyEnabled, false, nil)
		if res.Error() != nil || !res.Value {
			t.Fatalf("enabled = %v (err %v), want true from the synced config", res.Value, res.Error())
		}
	})
}
