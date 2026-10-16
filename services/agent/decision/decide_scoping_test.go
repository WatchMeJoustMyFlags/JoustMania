package decision

import (
	"testing"
	"time"
)

// UC: UC-040 A18 BR-011
func TestDispatchAddressesTheDecisionToTheDecidingGame(t *testing.T) {
	t.Run("UC-040/A18 end_game reaches the sink addressed to this loop's game", func(t *testing.T) {
		snap := remoteSnapshot(time.Second)
		snap.InterventionsAllowed = append(snap.InterventionsAllowed, InterventionEndGame)
		h := newRemoteHarness(t, snap, newFakeDecideBackend(ffaAnswer(InterventionEndGame, 0.95, "none", 0.5, "end it")))
		h.run(t)

		got := h.sink.decisions()
		if len(got) != 1 || got[0].Intervention != InterventionEndGame {
			t.Fatalf("applied = %+v, want one end_game", got)
		}
		if got[0].GameID != remoteGameID {
			t.Fatalf("end_game GameID = %q, want the deciding game %q", got[0].GameID, remoteGameID)
		}
	})
}
