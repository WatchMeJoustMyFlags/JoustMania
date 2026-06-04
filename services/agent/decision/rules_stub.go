package decision

import (
	"context"

	"github.com/joustmania/agent/gamecontext"
)

// NoopRules is the scaffold rules engine: it never proposes an intervention.
// It is superseded by ObjectiveRules (#726) but retained for tests.
type NoopRules struct{}

// Evaluate always returns nil (no decisions).
func (NoopRules) Evaluate(context.Context, gamecontext.GameContext) []Decision { return nil }
