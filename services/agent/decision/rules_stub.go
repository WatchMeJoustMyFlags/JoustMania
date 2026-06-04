package decision

import (
	"context"

	"github.com/joustmania/agent/gamecontext"
)

// NoopRules is the scaffold rules engine: it never proposes an intervention.
// It will be replaced by the real rules engine in issue #726.
type NoopRules struct{}

// Evaluate always returns nil (no decisions).
func (NoopRules) Evaluate(context.Context, gamecontext.GameContext) []Decision { return nil }
