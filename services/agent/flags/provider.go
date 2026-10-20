package flags

import (
	"context"
	"log/slog"
	"net"
	"strconv"
	"time"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

// Domain is the OpenFeature named domain / flagd flagSetId for agent flags.
// It matches metadata.flagSetId in services/flagd/agent.json.
const Domain = "agent"

// RolloutDomain is the domain / flagSetId of the rollout set, of which the agent reads
// only remediation_allowed (services/flagd/rollout.json).
const RolloutDomain = "rollout"

// Sync-stream reconnect backoff after a lost stream (flags.md §1.1): the provider's
// default caps it at 12 s, which would hold the agent fail-closed that long after a
// flagd restart.
const (
	syncRetryBackoffMs    = 500
	syncRetryBackoffMaxMs = 1000
)

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
		return flagd.NewProvider(
			flagd.WithInProcessResolver(),
			flagd.WithHost(cfg.Host),
			flagd.WithPort(cfg.Port),
			flagd.WithSelector("flagSetId="+flagSet),
			// syncedProvider passes noInitDeadline: the default 500 ms would wedge it (#1713).
			flagd.WithDeadline(int(initDeadline.Milliseconds())),
			flagd.WithRetryBackoffMs(syncRetryBackoffMs),
			flagd.WithRetryBackoffMaxMs(syncRetryBackoffMaxMs),
		)
	}
}

// SetupFlagd registers the agent's flagd providers (in-process resolver, #1713) for the
// `agent` and `rollout` domains and returns a Flags wrapper, a hot-reloadable
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
	return setupFlagd(ctx, flagdFactory(cfg, Domain), flagdFactory(cfg, RolloutDomain), probe, defaultSyncTiming, log)
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

func setupFlagd(ctx context.Context, agentSet, rolloutSet innerFactory, probe func() bool, timing syncTiming, log *slog.Logger) (*Flags, *LifecycleHolder, func(), error) {
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
	f := New(client, log)
	f.rollout = rollout

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
	client := openfeature.GetApiInstance().GetNamedClient(domain)
	// Standard OpenFeature OTel hooks (#1427): evaluation metrics and span events. On the
	// client, not the API, so a second SetupFlagd (tests) does not stack global hooks.
	hooks, err := NewOTelHooks()
	if err != nil {
		return nil, err
	}
	client.AddHooks(hooks...)
	return client, nil
}
