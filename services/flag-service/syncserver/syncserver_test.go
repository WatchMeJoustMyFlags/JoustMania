package syncserver

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	syncv1grpc "buf.build/gen/go/open-feature/flagd/grpc/go/flagd/sync/v1/syncv1grpc"
	syncv1 "buf.build/gen/go/open-feature/flagd/protocolbuffers/go/flagd/sync/v1"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/telemetry"
)

type harness struct {
	st     *state.State
	client syncv1grpc.FlagSyncServiceClient
	reader *sdkmetric.ManualReader
}

func newHarness(t *testing.T, served []string) *harness {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Join(filepath.Dir(file), "..")
	base, err := baseline.Load(filepath.Join(root, "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	reg, err := registry.LoadFile(filepath.Join(root, "registry.yaml"), base)
	if err != nil {
		t.Fatal(err)
	}
	st, _, err := state.New(base, reg, state.Options{Epoch: "e1", Served: served, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		t.Fatal(err)
	}
	reader := sdkmetric.NewManualReader()
	metrics, err := telemetry.NewMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter("test"), st.Stats)
	if err != nil {
		t.Fatal(err)
	}
	lis := bufconn.Listen(1 << 20)
	srv := grpc.NewServer(ServerOptions()...)
	(&Server{State: st, Metrics: metrics, Tracer: tracenoop.NewTracerProvider().Tracer("t"), Links: &telemetry.Links{}}).Register(srv)
	go func() { _ = srv.Serve(lis) }()
	t.Cleanup(srv.Stop)
	conn, err := grpc.NewClient("passthrough:///bufnet",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return lis.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return &harness{st: st, client: syncv1grpc.NewFlagSyncServiceClient(conn), reader: reader}
}

func flagSets(t *testing.T, payload string) map[string]string {
	t.Helper()
	var cfg struct {
		Flags map[string]struct {
			Metadata map[string]any `json:"metadata"`
		} `json:"flags"`
	}
	if err := json.Unmarshal([]byte(payload), &cfg); err != nil {
		t.Fatal(err)
	}
	out := map[string]string{}
	for k, f := range cfg.Flags {
		out[k], _ = f.Metadata["flagSetId"].(string)
	}
	return out
}

func waitStreams(t *testing.T, st *state.State, n int) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for st.SyncStreams() != n {
		if time.Now().After(deadline) {
			t.Fatalf("sync streams %d, want %d", st.SyncStreams(), n)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func rejected(t *testing.T, reader *sdkmetric.ManualReader, reason string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "flagservice_sync_rejected_total" {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				if v, _ := dp.Attributes.Value("reason"); v.AsString() == reason {
					return dp.Value
				}
			}
		}
	}
	return 0
}

// UC: UC-053 main BR-007
func TestSyncFlags_OneStreamAllSets(t *testing.T) {
	t.Run("UC-053/main an empty selector gets every served set, tagged per flag", func(t *testing.T) {
		h := newHarness(t, []string{"system", "observability"})
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		stream, err := h.client.SyncFlags(ctx, &syncv1.SyncFlagsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		msg, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		sets := flagSets(t, msg.GetFlagConfiguration())
		seen := map[string]bool{}
		for key, set := range sets {
			if set == "" {
				t.Errorf("%s has no metadata.flagSetId", key)
			}
			seen[set] = true
		}
		if !seen["system"] || !seen["observability"] || len(seen) != 2 {
			t.Fatalf("sets in the payload: %v", seen)
		}
		if msg.HasSyncContext() {
			t.Fatal("the service sends no sync_context")
		}
		waitStreams(t, h.st, 1)
	})
	t.Run("UC-053/main a write pushes a new configuration on the open stream", func(t *testing.T) {
		h := newHarness(t, []string{"observability"})
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		stream, err := h.client.SyncFlags(ctx, &syncv1.SyncFlagsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := stream.Recv(); err != nil {
			t.Fatal(err)
		}
		waitStreams(t, h.st, 1)
		if _, ferr := h.st.Put(state.WriteRequest{Set: "observability", Flag: "flagservice.propagation_probe", Value: json.RawMessage(`"n1"`)}); ferr != nil {
			t.Fatal(ferr)
		}
		msg, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if !json.Valid([]byte(msg.GetFlagConfiguration())) || !strings.Contains(msg.GetFlagConfiguration(), `"n1"`) {
			t.Fatalf("pushed configuration lacks the write: %.200s", msg.GetFlagConfiguration())
		}
	})
	t.Run("UC-053/A7 a closed stream no longer counts as open", func(t *testing.T) {
		h := newHarness(t, []string{"observability"})
		ctx, cancel := context.WithCancel(context.Background())
		stream, err := h.client.SyncFlags(ctx, &syncv1.SyncFlagsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := stream.Recv(); err != nil {
			t.Fatal(err)
		}
		waitStreams(t, h.st, 1)
		cancel()
		waitStreams(t, h.st, 0)
	})
}

// UC: UC-053 main
func TestSyncFlags_Selectors(t *testing.T) {
	h := newHarness(t, []string{"system", "observability"})
	t.Run("UC-053/main the flagd-selector header narrows to one set", func(t *testing.T) {
		ctx := metadata.AppendToOutgoingContext(context.Background(), "Flagd-Selector", "flagSetId=system")
		resp, err := h.client.FetchAllFlags(ctx, &syncv1.FetchAllFlagsRequest{})
		if err != nil {
			t.Fatal(err)
		}
		for key, set := range flagSets(t, resp.GetFlagConfiguration()) {
			if set != "system" {
				t.Fatalf("%s from set %s", key, set)
			}
		}
	})
	t.Run("UC-053/main the body selector works when no header is sent", func(t *testing.T) {
		req := &syncv1.FetchAllFlagsRequest{}
		req.SetSelector("flagSetId=observability")
		resp, err := h.client.FetchAllFlags(context.Background(), req)
		if err != nil {
			t.Fatal(err)
		}
		if sets := flagSets(t, resp.GetFlagConfiguration()); len(sets) == 0 || sets["grpc_rpc_spans"] != "observability" {
			t.Fatalf("sets %v", sets)
		}
	})
	t.Run("UC-053/main an unknown selector key is INVALID_ARGUMENT and counted", func(t *testing.T) {
		req := &syncv1.SyncFlagsRequest{}
		req.SetSelector("source=/etc/flagd/system.json")
		stream, err := h.client.SyncFlags(context.Background(), req)
		if err == nil {
			_, err = stream.Recv()
		}
		if status.Code(err) != codes.InvalidArgument {
			t.Fatalf("got %v, want InvalidArgument", err)
		}
		if n := rejected(t, h.reader, telemetry.RejectUnknownSel); n != 1 {
			t.Fatalf("unknown_selector counted %d", n)
		}
	})
	t.Run("UC-053/main a set that is not served is INVALID_ARGUMENT", func(t *testing.T) {
		ctx := metadata.AppendToOutgoingContext(context.Background(), "flagd-selector", "flagSetId=agent")
		_, err := h.client.FetchAllFlags(ctx, &syncv1.FetchAllFlagsRequest{})
		if status.Code(err) != codes.InvalidArgument {
			t.Fatalf("got %v", err)
		}
		if rejected(t, h.reader, telemetry.RejectUnknownSet) != 1 {
			t.Fatal("unknown_flag_set not counted")
		}
	})
	t.Run("UC-053/main GetMetadata is unimplemented", func(t *testing.T) {
		_, err := h.client.GetMetadata(context.Background(), &syncv1.GetMetadataRequest{})
		if status.Code(err) != codes.Unimplemented {
			t.Fatalf("got %v", err)
		}
	})
}

// Configuration check (architecture note §5.4), not flow evidence: no UC tag.
func TestKeepalive(t *testing.T) {
	t.Run("the server pings (flagd v0.16.0 sets no client keepalive) and allows client pings", func(t *testing.T) {
		if KeepaliveParams.Time != 30*time.Second || KeepaliveParams.Timeout != 10*time.Second {
			t.Fatalf("server keepalive %+v", KeepaliveParams)
		}
		if KeepalivePolicy.MinTime != 10*time.Second || !KeepalivePolicy.PermitWithoutStream {
			t.Fatalf("enforcement policy %+v", KeepalivePolicy)
		}
		if len(ServerOptions()) != 2 {
			t.Fatal("ServerOptions must carry both keepalive options")
		}
	})
}

func TestParseSelector(t *testing.T) {
	cases := map[string]struct {
		set string
		ok  bool
	}{
		"": {"", true}, "flagSetId=agent": {"agent", true}, " flagSetId = game ": {"game", true},
		"agent": {"", false}, "source=x": {"", false}, "flagSetId=": {"", false},
	}
	for in, want := range cases {
		set, ok := parseSelector(in)
		if set != want.set || ok != want.ok {
			t.Errorf("parseSelector(%q) = %q,%v want %q,%v", in, set, ok, want.set, want.ok)
		}
	}
}
