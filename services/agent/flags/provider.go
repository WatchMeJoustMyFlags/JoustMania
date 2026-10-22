package flags

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"strconv"
	"time"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/keepalive"
)

// Domain is the OpenFeature named domain / flagd flagSetId for agent flags.
// It matches metadata.flagSetId in services/flagd/agent.json.
const Domain = "agent"

// RolloutDomain is the domain / flagSetId of the rollout set, of which the agent reads
// only remediation_allowed (services/flagd/rollout.json).
const RolloutDomain = "rollout"

// The game set's domain (GameDomain) is declared in game.go: the agent reads only the
// two shield flags of the charge ledger from it (UC-040 A31, #1837).

// Sync-stream reconnect backoff after a lost stream (flags.md §1.1): the provider's
// default caps it at 12 s, which would hold the agent fail-closed that long after a
// flagd restart.
const (
	syncRetryBackoffMs    = 500
	syncRetryBackoffMaxMs = 1000
)

// Sync-stream keepalive (flags.md §1.1, #1768). flagd v0.16.0 serves the sync stream from
// a grpc-go server with no keepalive enforcement policy, so grpc-go's default applies: at
// most one client ping per 5 min (flagdServerMinPingInterval), and none without an active
// stream. go-sdk-contrib flagd v0.6.0 hard-coded a 30 s ping with PermitWithoutStream, so
// on an idle stream flagd counted ping strikes and sent GOAWAY too_many_pings every ~2-3
// min; each drop made the agent fail closed for about a second. v0.8.0 still hard-codes
// PermitWithoutStream, and its default keepalive time 0 ("off") is raised to grpc's 10 s
// minimum. The agent pings every 10 min (as lib/feature_flags.py does) and only while the
// stream is open.
const (
	flagdServerMinPingInterval = 5 * time.Minute
	syncKeepaliveTime          = 10 * time.Minute
	syncKeepaliveTimeout       = 5 * time.Second
)

// syncKeepaliveParams is the client keepalive the sync connection dials with.
var syncKeepaliveParams = keepalive.ClientParameters{
	Time:                syncKeepaliveTime,
	Timeout:             syncKeepaliveTimeout,
	PermitWithoutStream: false,
}

// syncDialOptions replaces the provider's own dial options, whose PermitWithoutStream
// cannot be turned off any other way (WithGrpcDialOptionsOverride replaces them all). It keeps what the provider
// would set for this config: plaintext transport (no TLS on the compose network), the
// sync-service retry policy with the agent's backoff, and the selector interceptors
// (which the provider appends to an override itself).
func syncDialOptions() []grpc.DialOption {
	return []grpc.DialOption{
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithKeepaliveParams(syncKeepaliveParams),
		grpc.WithDefaultServiceConfig(syncRetryServiceConfig()),
	}
}

// syncRetryServiceConfig mirrors v0.8.0's buildRetryPolicy (4 attempts) for the agent's
// backoff (the provider's 1 s to 5 s default would hold the agent fail-closed longer).
func syncRetryServiceConfig() string {
	seconds := func(ms int) string {
		return fmt.Sprint((time.Duration(ms) * time.Millisecond).Seconds()) + "s"
	}
	cfg := map[string]any{
		"methodConfig": []map[string]any{{
			"name": []map[string]string{{"service": "flagd.sync.v1.FlagSyncService"}},
			"retryPolicy": map[string]any{
				"MaxAttempts":          4,
				"InitialBackoff":       seconds(syncRetryBackoffMs),
				"MaxBackoff":           seconds(syncRetryBackoffMaxMs),
				"BackoffMultiplier":    2.0,
				"RetryableStatusCodes": []string{"UNKNOWN", "UNAVAILABLE"},
			},
		}},
	}
	out, _ := json.Marshal(cfg) // a map of plain values: cannot fail
	return string(out)
}

// ProviderConfig configures the flagd connection. Defaults mirror the compose
// network: flagd reachable as host "flagd" on its sync port 8015 (in-process resolver).
type ProviderConfig struct {
	Host string
	Port uint16
}

// flagdFactory builds go-sdk-contrib flagd in-process providers for one flag set: the
// flag configuration comes over flagd's sync stream (selector flagSetId=<set>, as the
// Python services select) and every read is a local evaluation.
func flagdFactory(cfg ProviderConfig, flagSet string) innerFactory {
	return func(initDeadline time.Duration) (innerProvider, error) {
		return flagd.NewProvider(flagdProviderOptions(cfg, flagSet, initDeadline)...)
	}
}

// flagdProviderOptions are the options every agent flagd provider is built with.
func flagdProviderOptions(cfg ProviderConfig, flagSet string, initDeadline time.Duration) []flagd.ProviderOption {
	return []flagd.ProviderOption{
		flagd.WithInProcessResolver(),
		flagd.WithHost(cfg.Host),
		flagd.WithPort(cfg.Port),
		flagd.WithSelector("flagSetId=" + flagSet),
		// syncedProvider passes noInitDeadline: the default 500 ms would wedge it (#1713).
		flagd.WithDeadline(int(initDeadline.Milliseconds())),
		flagd.WithRetryBackoffMs(syncRetryBackoffMs),
		flagd.WithRetryBackoffMaxMs(syncRetryBackoffMaxMs),
		// Keepalive within flagd's ping policy (#1768).
		flagd.WithGrpcDialOptionsOverride(syncDialOptions()),
	}
}

// SetupFlagd registers the agent's flagd providers (in-process resolver, #1713) for the
// `agent`, `rollout` and `game` (read-only, UC-040 A31) domains and returns a Flags wrapper, a hot-reloadable
// LifecycleHolder (#927), and a shutdown func.
//
// Setup never blocks startup: each provider waits for its first sync in the background
// (syncedProvider retries until flagd answers, however late) and until then, or while
// the sync stream is lost, every read returns its safe default: the agent comes up
// disabled (enabled=false) rather than failing to start (UC-045 A3).
//
// The LifecycleHolder is primed ONCE here with the four calibration flags
// (lifecycle.player_ttl/session_grace/evict_interval + decision.throttle). It is
// reloaded on every OpenFeature configuration change (#927; the in-process resolver
// emits one per flag sync) and, as the safety net that keeps a lost or never-started
// event path from freezing it (#1709), every LifecycleReloadInterval until ctx ends.
func SetupFlagd(ctx context.Context, cfg ProviderConfig, log *slog.Logger) (*Flags, *LifecycleHolder, func(), error) {
	probe := tcpProbe(net.JoinHostPort(cfg.Host, strconv.Itoa(int(cfg.Port))))
	return setupFlagd(ctx, flagdFactory(cfg, Domain), flagdFactory(cfg, RolloutDomain), flagdFactory(cfg, GameDomain), probe, defaultSyncTiming, log)
}

// tcpProbe reports whether addr accepts a TCP connection within 1 s.
func tcpProbe(addr string) func() bool {
	return func() bool {
		conn, err := net.DialTimeout("tcp", addr, time.Second)
		if err != nil {
			return false
		}
		_ = conn.Close()
		return true
	}
}

func setupFlagd(ctx context.Context, agentSet, rolloutSet, gameSet innerFactory, probe func() bool, timing syncTiming, log *slog.Logger) (*Flags, *LifecycleHolder, func(), error) {
	if log == nil {
		log = slog.Default()
	}
	client, err := registerDomain(Domain, agentSet, probe, timing, log)
	if err != nil {
		return nil, nil, nil, err
	}
	rollout, err := registerDomain(RolloutDomain, rolloutSet, probe, timing, log)
	if err != nil {
		return nil, nil, nil, err
	}
	game, err := registerDomain(GameDomain, gameSet, probe, timing, log)
	if err != nil {
		return nil, nil, nil, err
	}
	f := New(client, log)
	f.rollout = rollout
	f.game = game

	// Prime the holder before wiring the handler so consumers see correct values from
	// the first cycle even if no event ever fires.
	holder := NewLifecycleHolder(ctx, f, log)
	holder.RegisterConfigChangeHandler(client)
	go holder.Poll(ctx, LifecycleReloadInterval)

	shutdown := func() { openfeature.Shutdown() }
	return f, holder, shutdown, nil
}

// registerDomain sets a syncedProvider for one domain and returns its client. The
// SDK runs Init in the background (SetNamedProvider, non-blocking), so a late or
// unreachable flagd never wedges startup.
func registerDomain(domain string, factory innerFactory, probe func() bool, timing syncTiming, log *slog.Logger) (openfeature.IClient, error) {
	provider := newSyncedProvider(domain, factory, timing, log)
	provider.probe = probe
	if err := openfeature.SetNamedProvider(domain, provider); err != nil {
		return nil, err
	}
	client := openfeature.NewClient(domain)
	// Standard OpenFeature OTel hooks (#1427): evaluation metrics and span events. On the
	// client, not the API, so a second SetupFlagd (tests) does not stack global hooks.
	hooks, err := NewOTelHooks()
	if err != nil {
		return nil, err
	}
	client.AddHooks(hooks...)
	return client, nil
}
