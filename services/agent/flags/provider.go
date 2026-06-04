package flags

import (
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
// and returns a Flags wrapper plus a shutdown func. The RPC resolver (flagd's
// default) is used against the gRPC evaluation port (8013) so flag changes are
// observed live on every evaluation.
//
// Provider setup never blocks startup: if flagd is unreachable the provider
// still registers and every evaluation falls back to its safe default, so the
// agent comes up disabled (enabled=false) rather than failing to start.
func SetupFlagd(cfg ProviderConfig, log *slog.Logger) (*Flags, func(), error) {
	if log == nil {
		log = slog.Default()
	}

	provider, err := flagd.NewProvider(
		flagd.WithRPCResolver(),
		flagd.WithHost(cfg.Host),
		flagd.WithPort(cfg.Port),
	)
	if err != nil {
		return nil, nil, err
	}

	// SetNamedProvider (non-blocking) so an unreachable flagd never wedges
	// startup; the provider connects in the background and evaluations fall
	// back to defaults until it is READY.
	if err := openfeature.SetNamedProvider(Domain, provider); err != nil {
		return nil, nil, err
	}

	client := openfeature.GetApiInstance().GetNamedClient(Domain)
	shutdown := func() { openfeature.Shutdown() }
	return New(client, log), shutdown, nil
}
