package decision

import "context"

// NoopActions is the scaffold action sink: it discards decisions.
//
// The real sink (actions.Writer) writes game-scoped values and per-game event
// lists to the `interventions` flag set through the flag service (UC-053); it
// makes no gRPC calls to the game services. The context carries the active
// agent.action span so the write can be traced.
type NoopActions struct{}

// Apply discards the decision and never errors.
func (NoopActions) Apply(context.Context, Decision) error { return nil }
