package gamecontext

import (
	"testing"
	"time"
)

// UC: UC-042 BR-004
func TestStore_InterventionCountsUnboundedAndReset(t *testing.T) {
	clock := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	s := NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	s.SetGameActive(true)
	for i := 0; i < 2*timelineCap; i++ {
		s.AppendInterventionEvent("speed_up", "", false)
	}
	s.AppendInterventionEvent("grant_shield", "MOCK0001", true)

	snap := s.Snapshot()
	if got := snap.Session.InterventionCounts["speed_up"]; got.Dispatched != 2*timelineCap || got.Blocked != 0 {
		t.Errorf("UC-042/BR-004: speed_up tally = %+v, want %d dispatched (beyond the timeline cap)", got, 2*timelineCap)
	}
	if got := snap.Session.InterventionCounts["grant_shield"]; got.Blocked != 1 {
		t.Errorf("grant_shield tally = %+v, want 1 blocked", got)
	}
	// The snapshot is a copy: later appends do not change it.
	s.AppendInterventionEvent("speed_up", "", false)
	if snap.Session.InterventionCounts["speed_up"].Dispatched != 2*timelineCap {
		t.Error("snapshot shares the live tally map")
	}
	// A new game starts with an empty tally.
	s.SetGameActive(false)
	s.SetGameActive(true)
	if c := s.Snapshot().Session.InterventionCounts; len(c) != 0 {
		t.Errorf("tally not reset at game start: %+v", c)
	}
}
