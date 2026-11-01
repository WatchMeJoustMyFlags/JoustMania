package jev

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// UC: UC-040 A49 BR-038
func TestDescribePlayers_Waiting(t *testing.T) {
	t.Run("UC-040/A49 a waiting player is described as still in the game, not out", func(t *testing.T) {
		// A Fight Club round loser waiting for its next fight: waiting, not alive,
		// with an elimination order and no readings (#2513).
		st := withPlayer(t, func(p *decide.PlayerState) {
			*p = decide.PlayerState{Serial: p.Serial, Waiting: true, EliminationOrder: 2}
		})
		d := describePlayers(st)["MOCK0003"]
		if !strings.Contains(d, "Player MOCK0003, waiting for its fight, still in the game.") {
			t.Errorf("waiting player description = %q", d)
		}
		if strings.Contains(d, "eliminated") || strings.Contains(d, "out of the game") {
			t.Errorf("waiting player described as out: %q", d)
		}
	})
}
