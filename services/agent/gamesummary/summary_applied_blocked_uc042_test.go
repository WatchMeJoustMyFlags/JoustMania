package gamesummary

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// endedGame runs one game on a real Store, lets record feed its interventions, and
// returns the game-end snapshot the summary is built from.
func endedGame(t *testing.T, record func(s *gamecontext.Store)) gamecontext.GameContext {
	t.Helper()
	clock := time.Date(2026, 10, 20, 12, 0, 0, 0, time.UTC)
	s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	var snap gamecontext.GameContext
	s.OnGameEnd = func(c gamecontext.GameContext) { snap = c }
	s.SetGameActive(true)
	s.AdoptSessionID("game_fbf4cc5a372e")
	record(s)
	s.SetGameActive(false)
	if snap.SessionID == "" {
		t.Fatal("OnGameEnd did not fire")
	}
	return snap
}

// A fully gated game (dry run #2, #1765: 8 decisions, all not_allowed) reports
// 0 applied and 8 blocked, never "8 interventions" (#1771).
// UC: UC-042 main
func TestBuildSummary_FullyGatedGameAppliesNothing(t *testing.T) {
	snap := endedGame(t, func(s *gamecontext.Store) {
		for i := 0; i < 8; i++ {
			s.AppendInterventionEvent("grant_shield", "MOCK0006", true)
		}
	})
	got := BuildSummary(snap, BuildOptions{Now: time.Now()})
	if got.InterventionsApplied != 0 || got.InterventionsBlocked != 8 {
		t.Errorf("UC-042/main fully gated: applied=%d blocked=%d, want 0 / 8", got.InterventionsApplied, got.InterventionsBlocked)
	}
}

// Mixed game: applied counts only written interventions; blocked counts the
// permission chain's refusals plus actuation-gate no-ops; a failed write is in
// neither (entity model GAME_SUMMARY, #1771).
// UC: UC-042 main
func TestBuildSummary_MixedAppliedBlocked(t *testing.T) {
	snap := endedGame(t, func(s *gamecontext.Store) {
		// Two written.
		for i := 0; i < 2; i++ {
			s.AppendInterventionEvent("speed_up", "", false)
			s.RecordInterventionApply("speed_up", gamecontext.ApplyOutcomeApplied)
		}
		// One passed the permission chain, then interventions_enabled was off.
		s.AppendInterventionEvent("grant_shield", "AA:11", false)
		s.RecordInterventionApply("grant_shield", gamecontext.ApplyOutcomeGatedOff)
		// One passed every gate but the write failed.
		s.AppendInterventionEvent("adjust_volume", "", false)
		s.RecordInterventionApply("adjust_volume", gamecontext.ApplyOutcomeWriteFailed)
		// Two refused by the permission chain.
		s.AppendInterventionEvent("grant_shield", "BB:22", true)
		s.AppendInterventionEvent("eliminate_player", "BB:22", true)
	})
	got := BuildSummary(snap, BuildOptions{Now: time.Now()})
	if got.InterventionsApplied != 2 {
		t.Errorf("UC-042/main mixed: applied = %d, want 2", got.InterventionsApplied)
	}
	if got.InterventionsBlocked != 3 {
		t.Errorf("UC-042/main mixed: blocked = %d, want 3 (2 permission chain + 1 actuation gate)", got.InterventionsBlocked)
	}
	byType := map[string]InterventionTypeCount{}
	for _, c := range got.InterventionsByType {
		byType[c.Type] = c
	}
	if c := byType["grant_shield"]; c.Dispatched != 1 || c.Blocked != 1 || c.Applied != 0 || c.GatedOff != 1 {
		t.Errorf("grant_shield tally = %+v, want dispatched 1, blocked 1, applied 0, gated_off 1", c)
	}
	if c := byType["speed_up"]; c.Dispatched != 2 || c.Applied != 2 {
		t.Errorf("speed_up tally = %+v, want dispatched 2, applied 2", c)
	}
}
