package gamecontext

import (
	"testing"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

// round_loss_uc041_test.go covers #2517: a player_death span event whose
// death.eliminated is false (a Fight Club round loss, the loser stays in the game)
// is not an elimination; true or absent still is (UC-041 BR-008).

// deathSpan is a player span for serial with one player_death event; eliminated nil
// leaves the death.eliminated attribute out.
func deathSpan(serial string, eliminated *bool) ptrace.Traces {
	td := spanWith(map[string]string{"player.serial": serial}, nil)
	span := td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0)
	ev := span.Events().AppendEmpty()
	ev.SetName("player_death")
	if eliminated != nil {
		ev.Attributes().PutBool("death.eliminated", *eliminated)
	}
	return td
}

// UC: UC-041 BR-008
func TestApplySpans_RoundLossIsNoElimination(t *testing.T) {
	no, yes := false, true
	t.Run("UC-041/BR-008 death.eliminated false is not added to the sequence", func(t *testing.T) {
		s := newTestStore()
		if !s.ApplySpans(deathSpan("A", &no)) {
			t.Fatal("the span should still be recognized")
		}
		if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 0 {
			t.Fatalf("elimination seq = %v, want none for a round loss", seq)
		}
	})
	t.Run("UC-041/BR-008 death.eliminated true is an elimination", func(t *testing.T) {
		s := newTestStore()
		s.ApplySpans(deathSpan("A", &no))
		s.ApplySpans(deathSpan("A", &yes))
		if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 || seq[0] != "A" {
			t.Fatalf("elimination seq = %v, want [A]", seq)
		}
	})
	t.Run("UC-041/BR-008 a player_death without the attribute is an elimination", func(t *testing.T) {
		s := newTestStore()
		s.ApplySpans(deathSpan("A", nil))
		if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 1 || seq[0] != "A" {
			t.Fatalf("elimination seq = %v, want [A]", seq)
		}
	})
}
