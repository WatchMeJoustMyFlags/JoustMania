package gamecontext

import (
	"fmt"
	"reflect"
	"testing"
	"time"
)

// waiting_retain_uc041_test.go covers #2513: a Fight Club or Tournament player
// waiting for its fight exports no gauges (UC-041 BR-006), so the TTL evicts it
// mid-game; the Store remembers its whole-game aggregates, lists it as waiting and
// restores it into the game-end snapshot (UC-041 A2, BR-007; UC-040 A47).

const retainTTL = 5 * time.Second

type retainClock struct{ t time.Time }

func (c *retainClock) now() time.Time          { return c.t }
func (c *retainClock) advance(d time.Duration) { c.t = c.t.Add(d) }

// seatPlayer gives serial an alive player with the three whole-game aggregates.
func seatPlayer(s *Store, serial string, skill, peak, varAgg float64) {
	s.SetPlayerAlive(serial, true)
	s.SetPlayerSkill(serial, skill, true)
	s.SetPlayerPeakAccel(serial, peak)
	s.SetPlayerVarianceAggregate(serial, varAgg)
}

// fightClubStore starts a game with players A..D, then lets only A and B (the
// fighters) send signals past the TTL, so C and D (waiting) are evicted.
func fightClubStore(t *testing.T) (*Store, *retainClock, *[]GameContext) {
	t.Helper()
	clk := &retainClock{t: time.Unix(50_000, 0)}
	s := NewStore(retainTTL, 15*time.Second, clk.now)
	var ends []GameContext
	s.OnGameEnd = func(c GameContext) { ends = append(ends, c) }
	s.SetGameActive(true)
	s.SetActivePlayerCount(4)
	seatPlayer(s, "A", 0.9, 3.1, 0.40)
	seatPlayer(s, "B", 0.3, 2.2, 0.20)
	seatPlayer(s, "C", 0.1, 1.5, 0.10)
	seatPlayer(s, "D", 0.6, 2.8, 0.30)
	clk.advance(retainTTL + time.Second)
	s.SetPlayerIntensity("A", 1.1)
	s.SetPlayerIntensity("B", 1.2)
	s.EvictStale()
	return s, clk, &ends
}

func serialsOf(players map[string]*PlayerSignals) []string {
	out := make([]string, 0, len(players))
	for _, want := range []string{"A", "B", "C", "D"} {
		if players[want] != nil {
			out = append(out, want)
		}
	}
	return out
}

// UC: UC-041 A2 BR-007
func TestWaitingRetain_FightClub(t *testing.T) {
	t.Run("UC-041/BR-007 an evicted waiting player is listed as waiting, not as a player", func(t *testing.T) {
		s, _, _ := fightClubStore(t)
		snap := s.Snapshot()
		if got := serialsOf(snap.Players); !reflect.DeepEqual(got, []string{"A", "B"}) {
			t.Fatalf("live players = %v, want [A B]", got)
		}
		if !reflect.DeepEqual(snap.Waiting, []string{"C", "D"}) {
			t.Fatalf("Waiting = %v, want [C D]", snap.Waiting)
		}
	})

	t.Run("UC-041/BR-007 the game-end snapshot holds every player with its aggregates", func(t *testing.T) {
		s, _, ends := fightClubStore(t)
		s.SetGameActive(false)
		if len(*ends) != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", len(*ends))
		}
		end := (*ends)[0]
		if got := serialsOf(end.Players); !reflect.DeepEqual(got, []string{"A", "B", "C", "D"}) {
			t.Fatalf("end snapshot players = %v, want all four", got)
		}
		c := end.Players["C"]
		if !c.Waiting || c.Active != nil {
			t.Errorf("C Waiting=%v Active=%v, want waiting with no alive state", c.Waiting, c.Active)
		}
		if c.SkillLevel == nil || *c.SkillLevel != 0.1 || c.PeakAccel == nil || *c.PeakAccel != 1.5 ||
			c.MovementVarianceAggregate == nil || *c.MovementVarianceAggregate != 0.10 {
			t.Errorf("C aggregates = skill %v peak %v varAgg %v, want 0.1 / 1.5 / 0.10", c.SkillLevel, c.PeakAccel, c.MovementVarianceAggregate)
		}
		if a := end.Players["A"]; a.Waiting {
			t.Errorf("fighter A marked waiting")
		}
	})

	t.Run("UC-041/A2 a re-seated waiting player is live again with its aggregates", func(t *testing.T) {
		s, _, _ := fightClubStore(t)
		s.SetPlayerAlive("C", true)
		snap := s.Snapshot()
		if !reflect.DeepEqual(snap.Waiting, []string{"D"}) {
			t.Fatalf("Waiting = %v, want [D]", snap.Waiting)
		}
		c := snap.Players["C"]
		if c == nil || c.Waiting || c.Active == nil || !*c.Active {
			t.Fatalf("C = %+v, want a live alive player", c)
		}
		if c.SkillLevel == nil || *c.SkillLevel != 0.1 || c.PeakAccel == nil || *c.PeakAccel != 1.5 {
			t.Errorf("C aggregates = skill %v peak %v, want the remembered 0.1 / 1.5", c.SkillLevel, c.PeakAccel)
		}
	})

	t.Run("UC-041/BR-007 a waiting player who disconnects leaves the memory through its alive 0", func(t *testing.T) {
		// In the game's partition the disconnect arrives as game_player_alive 0
		// (Fight Club _leave_rotation, Tournament's elimination), not as
		// controller_connected, which carries no game id.
		s, _, ends := fightClubStore(t)
		s.SetPlayerAlive("C", false)
		snap := s.Snapshot()
		if !reflect.DeepEqual(snap.Waiting, []string{"D"}) {
			t.Fatalf("Waiting = %v, want [D]", snap.Waiting)
		}
		if c := snap.Players["C"]; c == nil || c.Waiting || c.Active == nil || *c.Active {
			t.Fatalf("C = %+v, want a live player that is not alive", c)
		}
		s.SetGameActive(false)
		c := (*ends)[0].Players["C"]
		if c == nil || c.Waiting || c.Active == nil || *c.Active {
			t.Fatalf("end snapshot C = %+v, want out, not waiting", c)
		}
	})

	t.Run("UC-041/BR-007 a live snapshot after the game ended lists no waiting players", func(t *testing.T) {
		s, _, _ := fightClubStore(t)
		s.SetGameActive(false) // inside the 15 s grace: retained is not reset yet
		if w := s.Snapshot().Waiting; w != nil {
			t.Fatalf("Waiting after the end = %v, want none", w)
		}
	})

	// Not a production path: controller_connected carries no game_id, so only the
	// fallback partition sees a disconnect. This pins the Store's guard there.
	t.Run("store guard: a disconnect seen by the fallback partition is not remembered", func(t *testing.T) {
		clk := &retainClock{t: time.Unix(50_000, 0)}
		s := NewStore(retainTTL, 15*time.Second, clk.now)
		s.SetGameActive(true)
		s.SetPlayerConnected("X", true)
		s.SetPlayerSkill("X", 0.5, true)
		s.SetPlayerConnected("X", false)
		s.EvictStale()
		if w := s.Snapshot().Waiting; w != nil {
			t.Fatalf("Waiting = %v, want none after a disconnect", w)
		}
	})

	t.Run("UC-041/BR-007 a player evicted after the game ended is not remembered", func(t *testing.T) {
		clk := &retainClock{t: time.Unix(50_000, 0)}
		s := NewStore(retainTTL, time.Hour, clk.now)
		s.SetGameActive(true)
		seatPlayer(s, "A", 0.5, 2, 0.2)
		s.SetGameActive(false)
		clk.advance(retainTTL + time.Second)
		s.EvictStale()
		if w := s.Snapshot().Waiting; w != nil {
			t.Fatalf("Waiting = %v, want none for a post-game eviction", w)
		}
	})

	t.Run("UC-041/BR-007 the next game and the grace reset forget the waiting players", func(t *testing.T) {
		s, clk, _ := fightClubStore(t)
		s.SetGameActive(false)
		clk.advance(16 * time.Second) // past the 15 s grace
		s.EvictStale()
		if w := s.Snapshot().Waiting; w != nil {
			t.Fatalf("Waiting after grace = %v, want none", w)
		}

		s2, _, _ := fightClubStore(t)
		s2.SetGameActive(false)
		s2.SetGameActive(true) // fallback store: a 0 -> 1 is a new game
		if w := s2.Snapshot().Waiting; w != nil {
			t.Fatalf("Waiting in the next game = %v, want none", w)
		}
	})

	t.Run("UC-041/BR-007 at most 64 players are remembered", func(t *testing.T) {
		clk := &retainClock{t: time.Unix(50_000, 0)}
		s := NewStore(retainTTL, 15*time.Second, clk.now)
		s.SetGameActive(true)
		for i := 0; i < maxRetainedPlayers+10; i++ {
			s.SetPlayerAlive(fmt.Sprintf("P%03d", i), true)
		}
		clk.advance(retainTTL + time.Second)
		s.EvictStale()
		if got := len(s.Snapshot().Waiting); got != maxRetainedPlayers {
			t.Fatalf("remembered %d players, want the cap %d", got, maxRetainedPlayers)
		}
	})
}
