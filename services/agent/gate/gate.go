// Package gate decides whether the agent should run its decision loop for the
// current GameContext.
package gate

import (
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// ShouldEvaluate gates the decision loop (issue #723 pseudocode:
// `if not should_evaluate(context): return`).
//
// Scaffold rule: a game is active AND at least one player is Active with fresh
// data (LastUpdate within playerTTL of now).
//
// Extension points for later issues (intentionally NOT implemented here):
//   - per-session cooldown / debounce so interventions don't fire too often
//   - a minimum player count before evaluation is worthwhile
//   - signal-completeness requirements (e.g. require battery + skill present)
//   - per-rule eligibility so individual rules can opt in/out
func ShouldEvaluate(c gamecontext.GameContext, now time.Time, playerTTL time.Duration) bool {
	if c.Session.GameActive == nil || !*c.Session.GameActive {
		return false
	}
	for _, p := range c.Players {
		if p.Active != nil && *p.Active && now.Sub(p.LastUpdate) <= playerTTL {
			return true
		}
	}
	return false
}
