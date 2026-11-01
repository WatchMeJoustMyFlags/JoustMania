package decide

import (
	"sort"

	"github.com/joustmania/agent/gamecontext"
)

// Stale readings in the decide state (UC-040 A50, BR-039, #2514). An alive player
// whose movement track is not live (FreshReadings: no readings, a frozen value or
// a newest value older than DangerSampleStaleAfter against the context's
// UpdatedAt) is marked ReadingsStale and its movement readings and skill are left
// out, so no backend reads an old value as current. The player stays listed and
// alive. The levers in freshReadingsLevers are those of the rules that skip such a
// player (R3, R4, R6, R8, R9; UC-040 A46, BR-037) and are never applied to a stale
// player from a backend answer either.

// freshReadingsLevers are the player-targeted levers whose rule targets only a
// player with fresh readings: adjust_player_sensitivity (R3, skill gap),
// grant_shield (R4, the weakest by skill, and R4d, sustained danger),
// eliminate_player (R6, the least-moving player, #2517, #2524) and
// send_controller_effect (R8, statue by movement variance; R9, random effect).
var freshReadingsLevers = map[string]bool{
	IntvAdjustPlayerSensitivity: true,
	IntvEliminatePlayer:         true,
	IntvGrantShield:             true,
	IntvSendControllerEffect:    true,
}

// NeedsFreshReadings reports whether intervention may only target a player whose
// readings are fresh (UC-040 BR-039).
func NeedsFreshReadings(intervention string) bool { return freshReadingsLevers[intervention] }

// FreshReadingsLevers returns the levers of NeedsFreshReadings, sorted.
func FreshReadingsLevers() []string {
	out := make([]string, 0, len(freshReadingsLevers))
	for name := range freshReadingsLevers {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// markStaleReadings marks an alive player whose readings are not fresh at the
// context's UpdatedAt and drops its movement readings and skill. Battery, the
// shield fields and the agent's own applied factors are kept: they are not
// movement readings.
func markStaleReadings(ps *PlayerState, p *gamecontext.PlayerSignals, c gamecontext.GameContext) {
	if !ps.Alive || FreshReadings(p.History, c.UpdatedAt) {
		return
	}
	ps.ReadingsStale = true
	ps.Intensity, ps.Variance, ps.Skill = nil, nil, nil
}

// TargetReadingsFresh reports whether serial is a player of c whose readings are
// fresh at c.UpdatedAt. A serial not in c is not fresh.
func TargetReadingsFresh(c gamecontext.GameContext, serial string) bool {
	p := c.Players[serial]
	return p != nil && FreshReadings(p.History, c.UpdatedAt)
}
