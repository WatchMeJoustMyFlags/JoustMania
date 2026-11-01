package decide

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// waiting_uc040_test.go covers #2513: the decide state lists a Fight Club or
// Tournament player waiting for its fight as waiting, not alive, with no readings
// (UC-040 A49, BR-038; decide-contract.md §1.2).

func waitingCtx(waiting []string) gamecontext.GameContext {
	tru, fls := true, false
	skill := 0.4
	c := gamecontext.GameContext{
		SessionID: "g-1",
		Players: map[string]*gamecontext.PlayerSignals{
			"A": {Serial: "A", Active: &tru, SkillLevel: &skill},
			"B": {Serial: "B", Active: &tru},
			"L": {Serial: "L", Active: &fls},
		},
		Waiting: waiting,
	}
	c.Session.EliminationSequence = []string{"L", "W1"}
	return c
}

// UC: UC-040 A49 BR-038
func TestBuildState_WaitingPlayers(t *testing.T) {
	st := BuildState(waitingCtx([]string{"W1", "W2"}))

	t.Run("UC-040/A49 waiting players are listed, not alive, without readings", func(t *testing.T) {
		got := map[string]PlayerState{}
		for _, p := range st.Players {
			got[p.Serial] = p
		}
		for _, s := range []string{"W1", "W2"} {
			p, ok := got[s]
			if !ok {
				t.Fatalf("%s missing from the state", s)
			}
			if !p.Waiting || p.Alive || p.Skill != nil || p.Intensity != nil || p.Danger != "" || p.Shielded {
				t.Errorf("%s = %+v, want waiting, not alive, no readings or signals", s, p)
			}
		}
		if got["W1"].EliminationOrder != 2 {
			t.Errorf("W1 elimination order = %d, want 2 (lost a round)", got["W1"].EliminationOrder)
		}
		if got["A"].Waiting || got["L"].Waiting {
			t.Errorf("alive or eliminated player marked waiting")
		}
	})

	t.Run("UC-040/BR-038 waiting players are no target", func(t *testing.T) {
		alive := strings.Join(st.AliveSerials(), ",")
		if alive != "A,B" {
			t.Fatalf("AliveSerials = %s, want A,B", alive)
		}
		for _, q := range FFAQuestions(st, []string{"grant_shield"}) {
			if q.ID != QuestionFFATarget {
				continue
			}
			for _, o := range q.Options {
				if o == "W1" || o == "W2" {
					t.Fatalf("ffa.target options %v include a waiting player", q.Options)
				}
			}
		}
	})

	t.Run("UC-040/A49 narrative and digest name them", func(t *testing.T) {
		if !strings.Contains(st.Narrative, "session: alive=2 eliminated=2 waiting=2") {
			t.Errorf("session line lacks waiting=2:\n%s", st.Narrative)
		}
		if !strings.Contains(st.Narrative, "  W2 waiting\n") || !strings.Contains(st.Narrative, "  W1 waiting\n") {
			t.Errorf("player lines lack waiting status:\n%s", st.Narrative)
		}
		if st.Digest == BuildState(waitingCtx(nil)).Digest {
			t.Errorf("digest ignores waiting players")
		}
	})

	t.Run("UC-040/BR-038 a state without waiting players is unchanged", func(t *testing.T) {
		plain := BuildState(waitingCtx(nil))
		if strings.Contains(plain.Narrative, "waiting") {
			t.Errorf("narrative without waiting players mentions waiting:\n%s", plain.Narrative)
		}
		for _, p := range plain.Players {
			if p.Waiting {
				t.Errorf("%s waiting without a waiting entry", p.Serial)
			}
		}
	})

	t.Run("UC-040/A49 the game-end snapshot's restored players read waiting", func(t *testing.T) {
		c := waitingCtx(nil)
		c.Players["W1"] = &gamecontext.PlayerSignals{Serial: "W1", Waiting: true}
		end := BuildState(c)
		for _, p := range end.Players {
			if p.Serial == "W1" && (!p.Waiting || p.Alive) {
				t.Fatalf("W1 = %+v, want waiting", p)
			}
		}
	})
}
