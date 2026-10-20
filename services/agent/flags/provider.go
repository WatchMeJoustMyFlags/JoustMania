package flags

import (
	"context"
	"log/slog"

	flagd "github.com/open-feature/go-sdk-contrib/providers/flagd/pkg"
	"github.com/open-feature/go-sdk/openfeature"
)

// Domain is the OpenFeature named domain / flagd flagSetId for agent flags.
// It matches metadata.flagSetId in services/flagd/agent.json.
const Domain = "agent"

// ProviderConfig configures the flagd connection. Defaults mirror the compose
// network: flagd reachable as host "flagd" on its gRPC evaluation port 8013.
type ProviderConfig struct {
	Host string
	Port uint16
}

// SetupFlagd registers a flagd-backed OpenFeature provider for the agent domain
// and returns a Flags wrapper, a hot-reloadable LifecycleHolder (#927), and a
// shutdown func. The RPC resolver is used against the gRPC evaluation port (8013)
// WITHOUT its resolution cache, so every evaluation asks flagd and a flag-service
// write is seen on the next read (#1709).
//
// Why no cache (#1709): the resolver cache drops an entry only when its event
// stream delivers flagd's configuration_change. When flagd answers after the
// provider's 500 ms init deadline (compose starts the agent once flagd has merely
// started), the provider never starts forwarding events; the stream goroutine then
// blocks on its second event and every later change is lost, so the cache served
// the old `enabled` / `experiments_enabled` / `decision.backend` until a restart.
// An uncached read is one local RPC; the agent reads its flags about once per
// decision cycle per game (~1/s), well within what flagd serves.
//
// Provider setup never blocks startup: if flagd is unreachable the provider
// still registers and every evaluation falls back to its safe default, so the
// agent comes up disabled (enabled=false) rather than failing to start.
//
// The LifecycleHolder is primed ONCE here with the four calibration flags
// (lifecycle.player_ttl/session_grace/evict_interval + decision.throttle) and is
// then reloaded every LifecycleReloadInterval until ctx ends (#1709): the uncached
// RPC resolver emits no configuration-change events, so polling is what keeps it
// live. The OpenFeature configuration-change handler (#927) stays registered for a
// provider that does emit them. The consumers (store TTLs, eviction ticker,
// decision throttle) read the holder on their hot path, so retuning them needs no
// agent restart.
func SetupFlagd(ctx context.Context, cfg ProviderConfig, log *slog.Logger) (*Flags, *LifecycleHolder, func(), error) {
	if log == nil {
		log = slog.Default()
	}

	provider, err := flagd.NewProvider(
		flagd.WithRPCResolver(),
		// No resolution cache: it is only invalidated by the event stream, which can
		// wedge for good (#1709). Every read goes to flagd.
		flagd.WithoutCache(),
		flagd.WithHost(cfg.Host),
		flagd.WithPort(cfg.Port),
	)
	if err != nil {
		return nil, nil, nil, err
	}

	// SetNamedProvider (non-blocking) so an unreachable flagd never wedges
	// startup; the provider connects in the background and evaluations fall
	// back to defaults until it is READY.
	if err := openfeature.SetNamedProvider(Domain, provider); err != nil {
		return nil, nil, nil, err
	}

	client := openfeature.GetApiInstance().GetNamedClient(Domain)
	// Standard OpenFeature OTel hooks (#1427): evaluation metrics and span events. On the
	// client, not the API, so a second SetupFlagd (tests) does not stack global hooks.
	hooks, err := NewOTelHooks()
	if err != nil {
		return nil, nil, nil, err
	}
	client.AddHooks(hooks...)
	f := New(client, log)

	// Hot-reload the four calibration flags: polled (#1709), plus the OpenFeature
	// config-change listener (#927). Prime the holder before wiring the handler so consumers see
	// correct values from the first cycle even if no event ever fires.
	holder := NewLifecycleHolder(ctx, f, log)
	holder.RegisterConfigChangeHandler(client)
	go holder.Poll(ctx, LifecycleReloadInterval)

	shutdown := func() { openfeature.Shutdown() }
	return f, holder, shutdown, nil
}
