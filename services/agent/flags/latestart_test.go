package flags

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync/atomic"
	"testing"
	"time"

	"buf.build/gen/go/open-feature/flagd/grpc/go/flagd/sync/v1/syncv1grpc"
	syncv1 "buf.build/gen/go/open-feature/flagd/protocolbuffers/go/flagd/sync/v1"
	isync "github.com/open-feature/flagd/core/pkg/sync"
	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
	"google.golang.org/grpc"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
)

// pushSyncServer is a gRPC flagd sync service over a fakeFlagdSource: every stream gets
// the current configuration, then the configuration again on every change (as flagd
// pushes on each flag-file write). It records the selector header of every stream.
type pushSyncServer struct {
	syncv1grpc.UnimplementedFlagSyncServiceServer
	src       *fakeFlagdSource
	selectors chan string
}

func (s *pushSyncServer) SyncFlags(_ *syncv1.SyncFlagsRequest, stream grpc.ServerStreamingServer[syncv1.SyncFlagsResponse]) error {
	md, _ := metadata.FromIncomingContext(stream.Context())
	if v := md.Get("flagd-selector"); len(v) > 0 {
		select {
		case s.selectors <- v[0]:
		default:
		}
	}
	for {
		_, enabled, changed := s.src.snapshot()
		if err := stream.Send(&syncv1.SyncFlagsResponse{FlagConfiguration: s.src.config(enabled)}); err != nil {
			return err
		}
		select {
		case <-changed:
		case <-stream.Context().Done():
			return nil
		}
	}
}

// lateSyncServer holds its listener from the start (no other process can take the port)
// but serves the sync service only from serve() on: until then the port accepts TCP
// connections that never get an HTTP/2 answer, like a flagd that is still starting.
type lateSyncServer struct {
	lis  net.Listener
	srv  *grpc.Server
	sync *pushSyncServer
	port uint16
}

func newLateSyncServer(t *testing.T) *lateSyncServer {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	// flagd v0.16.0's ping policy (grpc-go default), as in keepalive_test.go.
	srv := grpc.NewServer(grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{
		MinTime:             flagdServerMinPingInterval,
		PermitWithoutStream: false,
	}))
	fake := &pushSyncServer{src: newFakeFlagdSource("on"), selectors: make(chan string, 64)}
	syncv1grpc.RegisterFlagSyncServiceServer(srv, fake)
	// Stop, not GracefulStop: a wedged client stream must not hold the cleanup.
	t.Cleanup(func() { srv.Stop(); _ = lis.Close() })
	return &lateSyncServer{lis: lis, srv: srv, sync: fake, port: uint16(lis.Addr().(*net.TCPAddr).Port)}
}

func (l *lateSyncServer) serve() { go func() { _ = l.srv.Serve(l.lis) }() }

// eventually reports whether read returns want within timeout.
func eventually(read func() bool, want bool, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for read() != want {
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(5 * time.Millisecond)
	}
	return true
}

// TestFlagdFactory_LateSyncServerReachesReadyAndSeesEveryFlip drives the agent's real
// provider stack (flagdFactory with its dial options, syncedProvider, go-sdk) against a
// real gRPC sync server that comes up late (#1942): first the sync port probe fails, then
// the port accepts TCP without serving for longer than the provider's 500 ms default Init
// deadline, then it serves. Reads fail closed until READY,
// the agent builds ONE provider (its Init waits for the first sync instead of timing out
// and rebuilding), and every later flip, more than the upstream event buffer of 5, is seen.
//
// UC: UC-045 main A3
func TestFlagdFactory_LateSyncServerReachesReadyAndSeesEveryFlip(t *testing.T) {
	late := newLateSyncServer(t)
	cfg := ProviderConfig{Host: "127.0.0.1", Port: late.port}
	var flagdUp atomic.Bool
	var agentBuilds atomic.Int32
	agentFactory := flagdFactory(cfg, Domain)
	counted := func(initDeadline time.Duration) (innerProvider, error) {
		agentBuilds.Add(1)
		return agentFactory(initDeadline)
	}

	// Only the agent domain dials the gRPC server: go-sdk-contrib flagd v0.8.0 (and v0.6.0)
	// rewrites a package-level map in every gRPC sync Init (nonRetryableCodes), so two
	// providers starting at once are a data race under -race. The other two domains sync
	// from fake sources that come up with the server.
	rolloutSrc, gameSrc := newFakeFlagdSource("on"), newFakeFlagdSource("on")
	ctx, cancel := context.WithCancel(context.Background())
	f, holder, shutdown, err := setupFlagd(ctx, counted, fakeFactory(rolloutSrc), fakeFactory(gameSrc),
		flagdUp.Load, testSyncTiming, nil)
	if err != nil {
		cancel()
		t.Fatalf("setupFlagd: %v", err)
	}
	t.Cleanup(func() {
		cancel()
		// Bounded: a wedged provider (the #1713 bug) blocks its Shutdown for good.
		done := make(chan struct{})
		go func() { shutdown(); close(done) }()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			t.Error("provider shutdown still blocked after 2s: its sync is wedged")
		}
	})
	read := func() bool { return f.Evaluate(context.Background()).Enabled }
	assertFailsClosed := func(t *testing.T, when string) {
		t.Helper()
		if read() {
			t.Fatalf("enabled read on %s", when)
		}
		if f.RemediationAllowed(context.Background()) {
			t.Fatalf("remediation_allowed read on %s", when)
		}
		client := openfeature.NewClient(Domain)
		if st := client.State(); st == openfeature.ReadyState {
			t.Fatalf("agent domain state = %s %s, want not READY", st, when)
		}
		details, _ := client.BooleanValueDetails(context.Background(), keyEnabled, false, openfeature.EvaluationContext{})
		if details.ErrorCode != openfeature.ProviderNotReadyCode {
			t.Fatalf("enabled error code = %q %s, want %q", details.ErrorCode, when, openfeature.ProviderNotReadyCode)
		}
	}

	t.Run("UC-045/A3 fails closed while flagd is down", func(t *testing.T) {
		time.Sleep(700 * time.Millisecond) // longer than the provider's default 500 ms init deadline
		assertFailsClosed(t, "while flagd is down")
		if n := agentBuilds.Load(); n != 0 {
			t.Fatalf("agent provider builds = %d while the probe fails, want 0", n)
		}
	})

	t.Run("UC-045/A3 fails closed while flagd accepts TCP but has not synced", func(t *testing.T) {
		flagdUp.Store(true)
		if !eventually(func() bool { return agentBuilds.Load() >= 1 }, true, 2*time.Second) {
			t.Fatal("no agent provider built after the probe succeeded")
		}
		time.Sleep(700 * time.Millisecond) // longer than the provider's default 500 ms init deadline
		assertFailsClosed(t, "before the first sync")
		if n := agentBuilds.Load(); n != 1 {
			t.Fatalf("agent provider builds = %d, want 1: Init must wait for the first sync, not time out (#1713)", n)
		}
	})

	t.Run("UC-045/main READY and every flip after flagd serves", func(t *testing.T) {
		late.serve()
		rolloutSrc.set(true, "on")
		gameSrc.set(true, "on")
		waitBool(t, "enabled after the late first sync", read, true, 5*time.Second)
		waitBool(t, "remediation_allowed (rollout domain)", func() bool { return f.RemediationAllowed(context.Background()) }, true, 5*time.Second)
		if st := openfeature.NewClient(Domain).State(); st != openfeature.ReadyState {
			t.Fatalf("agent domain state = %s after the first sync, want READY", st)
		}
		seen := map[string]bool{}
		for deadline := time.After(5 * time.Second); !seen["flagSetId="+Domain]; {
			select {
			case sel := <-late.sync.selectors:
				seen[sel] = true
			case <-deadline:
				t.Fatalf("selectors seen %v, want flagSetId=%s", seen, Domain)
			}
		}
		for i := range 8 { // more than the upstream event buffer (5), where #1713 wedged
			late.sync.src.set(true, "off")
			waitBool(t, fmt.Sprintf("flip %d enabled off", i), read, false, time.Second)
			late.sync.src.set(true, "on")
			waitBool(t, fmt.Sprintf("flip %d enabled on", i), read, true, time.Second)
		}
		if n := agentBuilds.Load(); n != 1 {
			t.Fatalf("agent provider builds = %d, want 1", n)
		}
		deadline := time.Now().Add(3 * time.Second)
		for holder.PlayerTTL() != 3*time.Second {
			if time.Now().After(deadline) {
				t.Fatalf("lifecycle holder player_ttl = %s, want 3s from the sync", holder.PlayerTTL())
			}
			time.Sleep(10 * time.Millisecond)
		}
	})
}

// scriptSync is a sync stream (isync.ISync) that sends each configuration put on configs.
type scriptSync struct{ configs chan string }

func (s *scriptSync) Init(context.Context) error { return nil }
func (s *scriptSync) IsReady() bool              { return true }
func (s *scriptSync) ReSync(context.Context, chan<- isync.DataSync) error {
	return nil
}

func (s *scriptSync) Sync(ctx context.Context, out chan<- isync.DataSync) error {
	for {
		select {
		case cfg := <-s.configs:
			select {
			case out <- isync.DataSync{FlagData: cfg, Source: fakeSourceURI}:
			case <-ctx.Done():
				return ctx.Err()
			}
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// scriptedSyncProvider registers a syncedProvider over a real flagd in-process provider
// fed by a scriptSync under its own domain, after the first configuration is queued.
func scriptedSyncProvider(t *testing.T, domain, first string) (*scriptSync, *Flags) {
	t.Helper()
	src := &scriptSync{configs: make(chan string, 4)}
	src.configs <- first
	factory := func(initDeadline time.Duration) (innerProvider, error) {
		return flagd.NewProvider(
			flagd.WithInProcessResolver(),
			flagd.WithCustomSyncProviderAndUri(src, fakeSourceURI),
			flagd.WithDeadline(int(initDeadline.Milliseconds())),
		)
	}
	p := newSyncedProvider(domain, factory, testSyncTiming, nil)
	if err := openfeature.SetNamedProviderAndWait(domain, p); err != nil {
		t.Fatalf("SetNamedProviderAndWait: %v", err)
	}
	t.Cleanup(p.Shutdown)
	return src, New(openfeature.NewClient(domain), nil)
}

// TestSyncedProvider_IdenticalConfigAfterParseErrorIsServedAgain: after flagd sends a
// configuration the provider cannot parse (ERROR, reads fail closed), the next valid
// configuration serves reads again even when it equals the last good one (provider
// v0.8.0 re-sends READY; v0.6.0 waited for a configuration that changed a flag, #1942).
//
// UC: UC-045 A3
func TestSyncedProvider_IdenticalConfigAfterParseErrorIsServedAgain(t *testing.T) {
	good := newFakeFlagdSource("on").config("on")
	src, f := scriptedSyncProvider(t, "synced-provider-parse-error", good)
	read := func() bool { return f.Evaluate(context.Background()).Enabled }

	t.Run("UC-045/A3 a configuration that does not parse fails closed", func(t *testing.T) {
		waitBool(t, "enabled after the first sync", read, true, 2*time.Second)
		src.configs <- "{not json"
		waitBool(t, "enabled after an unparsable configuration", read, false, 2*time.Second)
	})
	t.Run("UC-045/A3 the identical good configuration is served again", func(t *testing.T) {
		src.configs <- good
		waitBool(t, "enabled after the identical good configuration", read, true, 2*time.Second)
	})
}

// TestSyncedProvider_DisabledKillSwitchReadsOff: a DISABLED `enabled` flag resolves to
// the caller's default (go-sdk v1.19 returns it without an error, open-feature/go-sdk#574),
// and the agent's default is off, so the kill switch stays closed (C-034, #1942).
//
// UC: UC-045 A3
func TestSyncedProvider_DisabledKillSwitchReadsOff(t *testing.T) {
	doc := map[string]any{"flags": map[string]any{
		keyEnabled: map[string]any{
			"state": "DISABLED", "defaultVariant": "on",
			"variants": map[string]any{"on": true, "off": false},
		},
	}}
	raw, _ := json.Marshal(doc)
	const domain = "synced-provider-disabled"
	_, f := scriptedSyncProvider(t, domain, string(raw))

	t.Run("UC-045/A3 a DISABLED enabled flag reads off", func(t *testing.T) {
		if DefaultEnabled {
			t.Fatal("DefaultEnabled = true: the kill switch must default to off")
		}
		details, err := openfeature.NewClient(domain).BooleanValueDetails(context.Background(), keyEnabled, true, openfeature.EvaluationContext{})
		if details.Reason != openfeature.DisabledReason {
			t.Fatalf("enabled reason = %q (err %v), want DISABLED: the flag must reach the provider", details.Reason, err)
		}
		if f.Evaluate(context.Background()).Enabled || f.AgentEnabled(context.Background()) {
			t.Fatal("a DISABLED enabled flag read on")
		}
	})
}

// TestUpstreamFlagdLateInit_StillWedges is an opt-in canary for the upstream bug the
// syncedProvider works around (#1709, #1713): a bare go-sdk-contrib flagd provider whose
// Init deadline passes before flagd serves never starts its event loop, so after a few
// flips its sync stops applying data. It passes while the bug is present and fails once
// upstream fixes it, the signal to re-evaluate syncedProvider (#1942).
//
// Opt-in (FLAGD_UPSTREAM_CANARY=1), run once and never with -count>1: the bare provider is
// never registered with the SDK (an SDK Shutdown would block on its wedged Shutdown) and
// its goroutines leak for the life of the test binary. No UC tag: it pins upstream
// behaviour, not the agent's.
func TestUpstreamFlagdLateInit_StillWedges(t *testing.T) {
	if os.Getenv("FLAGD_UPSTREAM_CANARY") != "1" {
		t.Skip("opt-in upstream canary: set FLAGD_UPSTREAM_CANARY=1 (leaks goroutines; never with -count>1)")
	}
	late := newLateSyncServer(t)
	p, err := flagd.NewProvider(flagdProviderOptions(ProviderConfig{Host: "127.0.0.1", Port: late.port}, Domain, 500*time.Millisecond)...)
	if err != nil {
		t.Fatalf("NewProvider: %v", err)
	}
	initErr := make(chan error, 1)
	go func() { initErr <- p.Init(openfeature.EvaluationContext{}) }()
	select {
	case err := <-initErr:
		if err == nil {
			t.Fatal("Init succeeded before flagd served")
		}
		t.Logf("bare provider Init failed as expected: %v", err)
	case <-time.After(5 * time.Second):
		t.Fatal("Init did not return after its 500 ms deadline")
	}

	late.serve()
	read := func() bool { return p.BooleanEvaluation(context.Background(), keyEnabled, false, nil).Value }
	if !eventually(read, true, 5*time.Second) {
		t.Logf("bare provider never applied the late first sync: upstream late-init bug still present")
		return
	}
	for i := range 10 {
		variant, want := "off", false
		if i%2 == 1 {
			variant, want = "on", true
		}
		late.sync.src.set(true, variant)
		if !eventually(read, want, 2*time.Second) {
			t.Logf("flip %d (%s) not seen within 2s: upstream late-init wedge still present", i+1, variant)
			return
		}
	}
	t.Fatal("every flip seen after a failed Init: upstream fixed the late-init wedge; re-evaluate syncedProvider (#1713, #1942)")
}
