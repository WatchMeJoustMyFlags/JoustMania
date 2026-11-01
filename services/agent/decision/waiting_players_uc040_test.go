package decision

import (
	"math"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// waiting_players_uc040_test.go covers #2513: in Fight Club and Tournament a player
// waiting for its fight is evicted from the context but still in the game, so the
// elimination fraction of R1, R2 and R4's elimination path divides by the
// game_active_players roster (UC-040 A49, BR-038).

func intp(v int) *int { return &v }

// tournamentAfterMatch1 is an 8-player Tournament after match 1: L1 lost (out),
// F1 and F2 fight match 2, the match-1 winner and four others wait (evicted).
func tournamentAfterMatch1(roster *int) gamecontext.GameContext {
	c := gameCtx(30, []string{"L1"}, map[string]testPlayer{
		"F1": {skill: fptr(0.2), active: true},
		"F2": {skill: fptr(0.9), active: true},
	})
	c.Players["L1"] = &gamecontext.PlayerSignals{Serial: "L1", Active: bptr(false)}
	c.Session.ActivePlayerCount = roster
	return c
}

func near(a, b float64) bool { return math.Abs(a-b) < 1e-9 }

// UC: UC-040 A49 BR-038
func TestWaitingPlayers_ElimFraction(t *testing.T) {
	t.Run("UC-040/A49 8-player Tournament after match 1 is 0.125", func(t *testing.T) {
		c := tournamentAfterMatch1(intp(8))
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 0.125) {
			t.Fatalf("elimFraction = %v, want 0.125 (1 of 8), not 1/3", got)
		}
	})
	t.Run("UC-040/BR-038 without a roster it is eliminations over eliminations + alive", func(t *testing.T) {
		c := tournamentAfterMatch1(nil)
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 1.0/3) {
			t.Fatalf("elimFraction = %v, want 1/3", got)
		}
	})
	t.Run("UC-040/BR-038 the end-of-game roster 0 falls back", func(t *testing.T) {
		c := tournamentAfterMatch1(intp(0))
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 1.0/3) {
			t.Fatalf("elimFraction = %v, want 1/3", got)
		}
	})
	t.Run("UC-040/BR-038 FFA (roster = eliminations + alive) is unchanged", func(t *testing.T) {
		c := gameCtx(30, []string{"X"}, map[string]testPlayer{"A": {active: true}, "B": {active: true}, "C": {active: true}})
		c.Session.ActivePlayerCount = intp(4)
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 0.25) {
			t.Fatalf("elimFraction = %v, want 0.25", got)
		}
	})
}

// UC: UC-040 A49 BR-038
func TestWaitingPlayers_RuleUrgency(t *testing.T) {
	fit := defaultFit()
	t.Run("UC-040/A49 R1 urgency uses the roster", func(t *testing.T) {
		cands := enduranceCandidates(tournamentAfterMatch1(intp(8)), fit)
		if len(cands) == 0 || cands[0].rule != "R1" {
			t.Fatalf("candidates = %v, want R1 first", interventionsOf(cands))
		}
		timeUrgency := (fit.EnduranceMinSessionSeconds - 30) / fit.EnduranceMinSessionSeconds
		if want := clamp01(timeUrgency * 2 * 0.125); !near(cands[0].urgency, want) {
			t.Fatalf("R1 urgency = %v, want %v (elimFraction 0.125)", cands[0].urgency, want)
		}
	})
	t.Run("UC-040/A49 R4 urgency uses the roster", func(t *testing.T) {
		c := tournamentAfterMatch1(intp(8))
		for _, cd := range balancedCandidates(c, fit) {
			if cd.rule != "R4" {
				continue
			}
			if want := clamp01((1 - 0.2) * 2 * 0.125); !near(cd.urgency, want) {
				t.Fatalf("R4 urgency = %v, want %v (elimFraction 0.125)", cd.urgency, want)
			}
			return
		}
		t.Fatal("no R4 candidate for F1")
	})
}

// UC: UC-040 A49 BR-038
func TestWaitingPlayers_FromStore(t *testing.T) {
	t.Run("UC-040/A49 the live store keeps the roster after waiting players are evicted", func(t *testing.T) {
		clock := time.Unix(70_000, 0)
		s := gamecontext.NewStore(5*time.Second, 15*time.Second, func() time.Time { return clock })
		s.SetGameActive(true)
		s.SetActivePlayerCount(8)
		for _, p := range []string{"P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"} {
			s.SetPlayerAlive(p, true)
		}
		clock = clock.Add(6 * time.Second)
		s.SetPlayerAlive("P2", false) // match-1 loser keeps its alive 0
		s.SetEliminationOrder("P2", 1)
		s.SetPlayerAlive("P3", true) // match 2 fighters
		s.SetPlayerAlive("P4", true)
		s.EvictStale()
		c := s.Snapshot()
		if n := len(activePlayers(c)); n != 2 {
			t.Fatalf("alive players in the context = %d, want 2 fighters", n)
		}
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 0.125) {
			t.Fatalf("elimFraction = %v, want 0.125", got)
		}
	})
}

// UC: UC-041 BR-007
func TestWaitingPlayers_EndSnapshotSkillGap(t *testing.T) {
	t.Run("UC-041/BR-007 Fight Club balanced skill gap sees the waiting players", func(t *testing.T) {
		clock := time.Unix(70_000, 0)
		s := gamecontext.NewStore(5*time.Second, 15*time.Second, func() time.Time { return clock })
		var end gamecontext.GameContext
		s.OnGameEnd = func(c gamecontext.GameContext) { end = c }
		s.SetGameActive(true)
		for serial, skill := range map[string]float64{"A": 0.5, "B": 0.3, "C": 0.1, "D": 0.9} {
			s.SetPlayerAlive(serial, true)
			s.SetPlayerSkill(serial, skill, true)
		}
		clock = clock.Add(6 * time.Second)
		s.SetPlayerIntensity("A", 1) // the last two fighters
		s.SetPlayerIntensity("B", 1)
		s.EvictStale()
		s.SetGameActive(false)
		if len(end.Players) != 4 {
			t.Fatalf("end snapshot players = %d, want 4", len(end.Players))
		}
		gap, ok := skillGap(end)
		if !ok || !near(gap, 0.8) {
			t.Fatalf("skill gap = %v (ok %v), want 0.8 over all four players, not 0.2 over the fighters", gap, ok)
		}
	})
}
