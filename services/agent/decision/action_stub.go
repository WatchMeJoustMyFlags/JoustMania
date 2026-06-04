package decision

import "context"

// NoopActions is the scaffold action sink: it discards decisions.
//
// The real implementation will WRITE OpenFeature intervention flag config to a
// dedicated flagd `interventions` domain — state-shaped flags for continuous
// interventions and nonce'd edge-triggered flags for one-shots — per
// docs/research/722-intervention-surface.md §8 / issue #730. It will NOT make
// gRPC calls to the game services. The context carries the active agent.action
// span so the flag write can be traced.
type NoopActions struct{}

// Apply discards the decision and never errors.
func (NoopActions) Apply(context.Context, Decision) error { return nil }
