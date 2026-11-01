package decide

import (
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// The shield_charged marker of the decide state (#1870, UC-040 main step 4 and A31
// step 1): the decision loop annotates the alive players its charge ledger holds a
// live entry for.

func playerByID(t *testing.T, st State, serial string) PlayerState {
	t.Helper()
	for _, p := range st.Players {
		if p.Serial == serial {
			return p
		}
	}
	t.Fatalf("player %s missing from %+v", serial, st.Players)
	return PlayerState{}
}

// UC: UC-040 main step 4 A31
func TestBuildState_ShieldCharged(t *testing.T) {
	t.Run("UC-040/A31 alive charged player is marked in field, narrative and digest", func(t *testing.T) {
		c := sampleCtx()
		st := BuildStateWith(c, Annotations{Charged: map[string]bool{"B": true}})
		if !playerByID(t, st, "B").ShieldCharged {
			t.Error("B: ShieldCharged = false, want true")
		}
		if playerByID(t, st, "A").ShieldCharged {
			t.Error("A: ShieldCharged = true, want false")
		}
		if !strings.Contains(st.Narrative, "intensity_rank=1/2 shield_charged\n") {
			t.Errorf("narrative lacks the shield_charged token:\n%s", st.Narrative)
		}
		if st.Digest == BuildState(c).Digest {
			t.Error("digest must change with shield_charged")
		}
	})
	t.Run("UC-040/A31 dead charged player is never marked", func(t *testing.T) {
		c := sampleCtx()
		st := BuildStateWith(c, Annotations{Charged: map[string]bool{"C": true}})
		if playerByID(t, st, "C").ShieldCharged {
			t.Error("C is out: ShieldCharged must stay false")
		}
		if strings.Contains(st.Narrative, "shield_charged") {
			t.Errorf("narrative must not mention a dead player's charge:\n%s", st.Narrative)
		}
		if st.Digest != BuildState(c).Digest {
			t.Error("a dead player's charge must not change the digest")
		}
	})
	t.Run("UC-040/step 4 empty annotations equal BuildState", func(t *testing.T) {
		c := sampleCtx()
		a, b := BuildStateWith(c, Annotations{}), BuildState(c)
		if a.Narrative != b.Narrative || a.Digest != b.Digest {
			t.Errorf("BuildStateWith(c, {}) differs from BuildState(c)")
		}
	})
}

// UC: UC-040 A31
func TestBuildState_ChargeSuppressesGrantShieldShielded(t *testing.T) {
	withEvent := func(detail string) gamecontext.GameContext {
		c := sampleCtx()
		c.Timeline = append(c.Timeline, gamecontext.TimelineEvent{
			At: c.UpdatedAt.Add(-3 * time.Second), Kind: gamecontext.EventIntervention, Detail: detail, Serial: "B",
		})
		return c
	}
	charged := Annotations{Charged: map[string]bool{"B": true}}

	t.Run("UC-040/A31 grant_shield to a charged player is not shielded", func(t *testing.T) {
		p := playerByID(t, BuildStateWith(withEvent("grant_shield"), charged), "B")
		if p.Shielded || !p.ShieldCharged {
			t.Errorf("shielded=%v shield_charged=%v, want false/true", p.Shielded, p.ShieldCharged)
		}
	})
	t.Run("UC-040/A31 toughness_boost to a charged player is still shielded", func(t *testing.T) {
		p := playerByID(t, BuildStateWith(withEvent("toughness_boost"), charged), "B")
		if !p.Shielded || !p.ShieldCharged {
			t.Errorf("shielded=%v shield_charged=%v, want true/true", p.Shielded, p.ShieldCharged)
		}
	})
	t.Run("UC-040/A31 grant_shield without a charge is shielded as before", func(t *testing.T) {
		p := playerByID(t, BuildStateWith(withEvent("grant_shield"), Annotations{}), "B")
		if !p.Shielded || p.ShieldCharged {
			t.Errorf("shielded=%v shield_charged=%v, want true/false", p.Shielded, p.ShieldCharged)
		}
	})
}

// TestDigest_ByteStableWithoutCharge pins the digest of fixed fixtures, computed on
// main before #1870: a state without a charge keeps a byte-identical digest. Re-pinned
// by #2514: sampleCtx gained a fresh reading for B (so B's player line carries
// "movement=▄"); no field or token changed otherwise.
//
// UC: UC-040 main step 4
func TestDigest_ByteStableWithoutCharge(t *testing.T) {
	t.Run("UC-040/step 4 plain fixture", func(t *testing.T) {
		const want = "f3aba7e7f210facf043b7c70f114c9496dbdee56fb770464617bdd3cbea80019"
		if got := BuildStateWith(sampleCtx(), Annotations{}).Digest; got != want {
			t.Errorf("digest = %s, want %s", got, want)
		}
	})
	t.Run("UC-040/step 4 shielded fixture", func(t *testing.T) {
		c := sampleCtx()
		c.Timeline = append(c.Timeline, gamecontext.TimelineEvent{
			At: c.UpdatedAt.Add(-4 * time.Second), Kind: gamecontext.EventIntervention, Detail: "grant_shield", Serial: "B",
		})
		const want = "153257f76b49376cdcab5c2eb942dffbc8f73fa08b17592483d081845712d557"
		if got := BuildStateWith(c, Annotations{}).Digest; got != want {
			t.Errorf("digest = %s, want %s", got, want)
		}
	})
}
