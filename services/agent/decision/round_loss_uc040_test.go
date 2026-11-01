package decision

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// round_loss_uc040_test.go covers #2517: a Fight Club round loss is not an
// elimination, so R1, R2 and R4's elimination fraction stays 0 while nobody is out
// of the game (UC-040 A49 step 2, BR-038; UC-041 BR-008).

// fcDeath is the coordinator's player_death span event for serial, with its
// death.eliminated attribute (Fight Club sets it on every death).
func fcDeath(serial string, eliminated bool) ptrace.Traces {
	td := ptrace.NewTraces()
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("player_lifecycle")
	span.Attributes().PutStr("player.serial", serial)
	ev := span.Events().AppendEmpty()
	ev.SetName("player_death")
	ev.Attributes().PutBool("death.eliminated", eliminated)
	return td
}

// UC: UC-040 A49 BR-038
func TestFightClubRoundLoss_ElimFraction(t *testing.T) {
	newFC := func() *gamecontext.Store {
		clock := time.Unix(80_000, 0)
		s := gamecontext.NewStore(5*time.Second, 15*time.Second, func() time.Time { return clock })
		s.SetGameActive(true)
		s.SetActivePlayerCount(4)
		for _, p := range []string{"P1", "P2", "P3", "P4"} {
			s.SetPlayerAlive(p, true)
		}
		return s
	}
	t.Run("UC-040/A49 4-player Fight Club, every player lost a round: fraction 0", func(t *testing.T) {
		s := newFC()
		for _, p := range []string{"P1", "P2", "P3", "P4"} {
			s.ApplySpans(fcDeath(p, false))
		}
		c := s.Snapshot()
		if seq := c.Session.EliminationSequence; len(seq) != 0 {
			t.Fatalf("elimination sequence = %v, want none (round losses)", seq)
		}
		if got := elimFraction(c, len(activePlayers(c))); got != 0 {
			t.Fatalf("elimFraction = %v, want 0: nobody is out of the game", got)
		}
	})
	t.Run("UC-041/BR-008 a player who left the rotation still counts: 1 of 4", func(t *testing.T) {
		s := newFC()
		for _, p := range []string{"P1", "P2", "P3", "P4"} {
			s.ApplySpans(fcDeath(p, false))
		}
		s.ApplySpans(fcDeath("P2", true)) // P2 disconnected while waiting: out for good
		s.SetEliminationOrder("P2", 1)
		s.SetPlayerAlive("P2", false)
		c := s.Snapshot()
		if seq := c.Session.EliminationSequence; len(seq) != 1 || seq[0] != "P2" {
			t.Fatalf("elimination sequence = %v, want [P2]", seq)
		}
		if got := elimFraction(c, len(activePlayers(c))); !near(got, 0.25) {
			t.Fatalf("elimFraction = %v, want 0.25 (1 of 4)", got)
		}
	})
}
