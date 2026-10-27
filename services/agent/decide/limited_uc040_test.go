package decide

import (
	"encoding/json"
	"strings"
	"testing"
)

// The shield_limited marker and the applied per-player levels of the decide state
// (#2066; UC-040 main step 4, A33, A35, A37; decide-contract.md §1.2).

// UC: UC-040 main step 4 A33 A37
func TestBuildState_ShieldLimited(t *testing.T) {
	t.Run("UC-040/A33 a limited alive player is marked in field, wire, narrative and digest", func(t *testing.T) {
		c := sampleCtx()
		st := BuildStateWith(c, Annotations{NoShield: map[string]bool{"B": true}})
		b := playerByID(t, st, "B")
		if !b.ShieldLimited || b.ShieldCharged {
			t.Fatalf("B: limited=%v charged=%v, want limited only", b.ShieldLimited, b.ShieldCharged)
		}
		if playerByID(t, st, "A").ShieldLimited {
			t.Error("A: ShieldLimited = true, want false")
		}
		if !strings.Contains(st.Narrative, "intensity_rank=1/2 shield_limited\n") {
			t.Errorf("narrative lacks the bare shield_limited token:\n%s", st.Narrative)
		}
		raw, _ := json.Marshal(b)
		if !strings.Contains(string(raw), `"shield_limited":true`) {
			t.Errorf("wire lacks shield_limited: %s", raw)
		}
		if st.Digest == BuildState(c).Digest {
			t.Error("digest must change with shield_limited")
		}
	})
	t.Run("UC-040/A33 a charged player is shield_charged, not also shield_limited", func(t *testing.T) {
		st := BuildStateWith(sampleCtx(), Annotations{Charged: map[string]bool{"B": true}, NoShield: map[string]bool{"B": true}})
		b := playerByID(t, st, "B")
		if !b.ShieldCharged || b.ShieldLimited {
			t.Fatalf("B: charged=%v limited=%v, want charged only", b.ShieldCharged, b.ShieldLimited)
		}
		want := BuildStateWith(sampleCtx(), Annotations{Charged: map[string]bool{"B": true}})
		if st.Digest != want.Digest {
			t.Error("a charged player in NoShield must keep the shield_charged-only digest")
		}
	})
	t.Run("UC-040/A33 a dead player is never limited and an empty set keeps the digest", func(t *testing.T) {
		c := sampleCtx()
		st := BuildStateWith(c, Annotations{NoShield: map[string]bool{"C": true}})
		if playerByID(t, st, "C").ShieldLimited || strings.Contains(st.Narrative, "shield_limited") {
			t.Errorf("dead C marked limited:\n%s", st.Narrative)
		}
		if st.Digest != BuildState(c).Digest {
			t.Error("a dead player's limit must not change the digest")
		}
	})
	t.Run("UC-040/A37 in a hold game the token names the hold reason the state carries", func(t *testing.T) {
		c := withHoldGauges(sampleCtx(), "hold")
		a := *c.Players["A"]
		a.ShieldEnergy = fptr(4.6) // cap 5, margin 0.5: shield_pool_full
		c.Players["A"] = &a
		st := BuildStateWith(c, Annotations{NoShield: map[string]bool{"A": true}})
		if !strings.Contains(st.Narrative, " shield_limited=shield_pool_full") {
			t.Errorf("narrative lacks the hold reason:\n%s", st.Narrative)
		}
	})
}

// UC: UC-040 main step 4 A35
func TestBuildState_AppliedLevels(t *testing.T) {
	levels := map[string]string{
		LevelKey("adjust_player_sensitivity", "A"): "0.8",
		LevelKey("set_player_handicap", "A"):       "1.25",
		LevelKey("adjust_player_sensitivity", "B"): "1",   // neutral
		LevelKey("set_player_handicap", "B"):       "abc", // not a number
		LevelKey("adjust_player_sensitivity", "C"): "0.8", // dead
		LevelKey("adjust_global_sensitivity", ""):  "3",   // game-wide: not annotated
	}
	t.Run("UC-040/A35 a non-neutral applied factor is set and rendered", func(t *testing.T) {
		c := sampleCtx()
		st := BuildStateWith(c, Annotations{Levels: levels})
		a := playerByID(t, st, "A")
		if a.SensitivityFactor == nil || *a.SensitivityFactor != 0.8 || a.HandicapFactor == nil || *a.HandicapFactor != 1.25 {
			t.Fatalf("A factors = %v / %v, want 0.8 / 1.25", a.SensitivityFactor, a.HandicapFactor)
		}
		if !strings.Contains(st.Narrative, " sensitivity_factor=0.80 handicap_factor=1.25\n") {
			t.Errorf("narrative lacks the factor tokens:\n%s", st.Narrative)
		}
		raw, _ := json.Marshal(a)
		if !strings.Contains(string(raw), `"sensitivity_factor":0.8`) || !strings.Contains(string(raw), `"handicap_factor":1.25`) {
			t.Errorf("wire lacks the factors: %s", raw)
		}
		if st.Digest == BuildState(c).Digest {
			t.Error("digest must change with an applied factor")
		}
	})
	t.Run("UC-040/A35 neutral, unparsable, dead and game-wide values add nothing", func(t *testing.T) {
		c := sampleCtx()
		only := map[string]string{}
		for k, v := range levels {
			if !strings.HasSuffix(k, "|A") {
				only[k] = v
			}
		}
		st := BuildStateWith(c, Annotations{Levels: only})
		if st.Digest != BuildState(c).Digest || strings.Contains(st.Narrative, "_factor") {
			t.Errorf("state changed for values that must not be annotated:\n%s", st.Narrative)
		}
	})
}
