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
// shutdown func. The RPC resolver (flagd's default) is used against the gRPC
// evaluation port (8013) so flag changes are observed live on every evaluation.
//
// Provider setup never blocks startup: if flagd is unreachable the provider
// still registers and every evaluation falls back to its safe default, so the
// agent comes up disabled (enabled=false) rather than failing to start.
//
// The LifecycleHolder is primed ONCE here with the four calibration flags
// (lifecycle.player_ttl/session_grace/evict_interval + decision.throttle) and is
// kept live by a DOMAIN-scoped OpenFeature configuration-change handler (#927):
// flagd emits a config-change when its source changes, the Go SDK surfaces it as
// openfeature.ProviderConfigChange, and the handler re-evaluates the four flags
// into the holder. The consumers (store TTLs, eviction ticker, decision throttle)
// read the holder on their hot path, so retuning them needs no agent restart. ctx
// scopes the holder's flag evaluations.
func SetupFlagd(ctx context.Context, cfg ProviderConfig, log *slog.Logger) (*Flags, *LifecycleHolder, func(), error) {
	if log == nil {
		log = slog.Default()
	}

	provider, err := flagd.NewProvider(
		flagd.WithRPCResolver(),
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
	f := New(client, log)

	// Hot-reload the four calibration flags via the OpenFeature config-change
	// listener (#927). Prime the holder before wiring the handler so consumers see
	// correct values from the first cycle even if no event ever fires.
	holder := NewLifecycleHolder(ctx, f, log)
	holder.RegisterConfigChangeHandler(client)

	shutdown := func() { openfeature.Shutdown() }
	return f, holder, shutdown, nil
}
