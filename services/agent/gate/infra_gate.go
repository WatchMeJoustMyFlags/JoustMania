package gate

import (
	"time"

	"github.com/joustmania/agent/infracontext"
)

// ShouldEvaluateInfra gates the infrastructure (rollout) decision loop (#734).
// It returns true when at least one controller reported a fresh
// controller.bluetooth_health update (LastUpdate within ttl of now).
//
// Unlike ShouldEvaluate, this gate is GAME-STATE-INDEPENDENT: controllers
// connect and stream Bluetooth health in the lobby, before and after any game,
// and the rollout is expanded based on transport health alone. Gating on a live
// game here would stall the rollout whenever no game is running, which is most of
// the time. A stale-only context (every controller older than ttl) gates out: a
// rollout decision on signals nobody is currently producing would be acting on
// nothing.
func ShouldEvaluateInfra(infra infracontext.InfraContext, now time.Time, ttl time.Duration) bool {
	for _, c := range infra.Controllers {
		if c != nil && now.Sub(c.LastUpdate) <= ttl {
			return true
		}
	}
	return false
}
