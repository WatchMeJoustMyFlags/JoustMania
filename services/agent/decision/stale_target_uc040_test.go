package decision

import (
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// stale_target_uc040_test.go covers #2514: a remote answer that applies a lever
// needing fresh readings (decide.NeedsFreshReadings) to a player whose readings
// are stale when the answer lands is discarded with target_stale (UC-040 A50,
// BR-039; decide-contract.md §4.4). Other levers still act on that player.

// staleA makes A's only reading 10 s old at the landing context's UpdatedAt.
func staleA(h remoteHarness) {
	c := remoteContext()
	c.Players["A"].History = []gamecontext.MovementSample{{At: c.UpdatedAt.Add(-10 * time.Second), Intensity: 0.5}}
	h.provider.set(remoteGameID, c)
}

// UC: UC-040 A50 BR-039
func TestRemote_TargetStale(t *testing.T) {
	t.Run("UC-040/A50 grant_shield to a player with stale readings is discarded", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, time.Hour, staleA)
		assertDiscarded(t, h, span, DiscardTargetStale)
	})

	t.Run("UC-040/BR-039 a lever without a fresh-readings rule still acts on that player", func(t *testing.T) {
		snap := remoteSnapshot(time.Hour)
		snap.InterventionsAllowed = append(snap.InterventionsAllowed, decide.IntvSetPlayerHandicap)
		be := newFakeDecideBackend(ffaAnswer(decide.IntvSetPlayerHandicap, 0.95, "A", 0.5, "x"))
		h, span := fireAndMutateSnapshot(t, be, snap, time.Hour, staleA)
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
			t.Fatalf("outcome = %q, want act", g)
		}
		got := h.sink.decisions()
		if len(got) != 1 || got[0].Intervention != decide.IntvSetPlayerHandicap || got[0].TargetSerial != "A" {
			t.Fatalf("applied %+v, want one set_player_handicap to A", got)
		}
	})
}
