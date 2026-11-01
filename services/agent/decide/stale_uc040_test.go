package decide

import (
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// stale_uc040_test.go covers #2514: the decide state marks an alive player whose
// movement readings are not fresh (FreshReadings, the 3 s bound of R3, R4, R8)
// readings_stale and leaves out its readings and skill, without hiding it
// (UC-040 A50, BR-039; decide-contract.md §1.2).

// staleCtx: F has a live reading, S a reading 10 s old, N none at all, L is out.
func staleCtx() gamecontext.GameContext {
	base := time.Unix(1_700_000_000, 0)
	now := base.Add(60 * time.Second)
	c := gamecontext.GameContext{
		SessionID: "g-stale",
		UpdatedAt: now,
		Players: map[string]*gamecontext.PlayerSignals{
			"F": {Serial: "F", Active: bptr(true), MovementIntensity: fptr(0.5), MovementVariance: fptr(0.1), SkillLevel: fptr(0.6), BatteryPct: fptr(70),
				History: []gamecontext.MovementSample{{At: now.Add(-time.Second), Intensity: 0.5}}},
			"S": {Serial: "S", Active: bptr(true), MovementIntensity: fptr(2.2), MovementVariance: fptr(0.01), SkillLevel: fptr(0.1), BatteryPct: fptr(50),
				History: []gamecontext.MovementSample{{At: now.Add(-10 * time.Second), Intensity: 2.2}}},
			"N": {Serial: "N", Active: bptr(true), SkillLevel: fptr(0.9)},
			"L": {Serial: "L", Active: bptr(false), SkillLevel: fptr(0.3),
				History: []gamecontext.MovementSample{{At: base, Intensity: 1.0}}},
		},
	}
	c.Session.DeathThreshold = fptr(2.3)
	c.Session.EliminationSequence = []string{"L"}
	return c
}

// UC: UC-040 A50 BR-039
func TestBuildState_ReadingsStale(t *testing.T) {
	st := BuildState(staleCtx())

	t.Run("UC-040/A50 a stale alive player is marked and its readings are left out", func(t *testing.T) {
		for _, s := range []string{"S", "N"} {
			p := playerByID(t, st, s)
			if !p.ReadingsStale || !p.Alive {
				t.Fatalf("%s = %+v, want alive and readings_stale", s, p)
			}
			if p.Intensity != nil || p.Variance != nil || p.Skill != nil || p.Danger != "" || p.ThresholdMargin != nil || p.IntensityRank != 0 {
				t.Errorf("%s keeps readings or danger signals: %+v", s, p)
			}
		}
		if p := playerByID(t, st, "S"); p.BatteryPct == nil || *p.BatteryPct != 50 {
			t.Errorf("S battery = %v, want 50 (not a movement reading)", p.BatteryPct)
		}
		if st.Struggling == "S" || st.MostHeadroom == "S" {
			t.Errorf("stale S named most_headroom/struggling: %q/%q", st.MostHeadroom, st.Struggling)
		}
	})

	t.Run("UC-040/A50 a fresh player and an eliminated player are not marked", func(t *testing.T) {
		f := playerByID(t, st, "F")
		if f.ReadingsStale || f.Intensity == nil || f.Skill == nil || f.Variance == nil || f.Danger == "" {
			t.Errorf("fresh F = %+v, want its readings and danger signals", f)
		}
		if l := playerByID(t, st, "L"); l.ReadingsStale {
			t.Errorf("eliminated L marked readings_stale")
		}
	})

	t.Run("UC-040/A50 the stale player stays an ffa.target option", func(t *testing.T) {
		if got := strings.Join(st.AliveSerials(), ","); got != "F,N,S" {
			t.Fatalf("AliveSerials = %s, want F,N,S", got)
		}
	})

	t.Run("UC-040/A50 narrative token, no movement sparkline, digest field", func(t *testing.T) {
		if !strings.Contains(st.Narrative, "  S alive readings_stale battery=50.00\n") {
			t.Errorf("narrative lacks the stale S line:\n%s", st.Narrative)
		}
		if !strings.Contains(st.Narrative, "  N alive readings_stale\n") {
			t.Errorf("narrative lacks the stale N line:\n%s", st.Narrative)
		}
		if !strings.Contains(st.Narrative, "  F alive intensity=0.50 variance=0.10 skill=0.60 battery=70.00 movement=") {
			t.Errorf("narrative lacks the fresh F line:\n%s", st.Narrative)
		}
		c := staleCtx()
		c.Players["S"].History = []gamecontext.MovementSample{{At: c.UpdatedAt, Intensity: 2.2}}
		if BuildState(c).Digest == st.Digest {
			t.Error("digest must change when S's readings turn fresh")
		}
	})
}

// UC: UC-040 BR-039
func TestNeedsFreshReadings(t *testing.T) {
	t.Run("UC-040/BR-039 exactly the levers of R3, R4 and R8", func(t *testing.T) {
		want := []string{IntvAdjustPlayerSensitivity, IntvGrantShield, IntvSendControllerEffect}
		if got := FreshReadingsLevers(); !reflect.DeepEqual(got, want) {
			t.Fatalf("FreshReadingsLevers = %v, want %v", got, want)
		}
		for _, iv := range []string{IntvSetPlayerHandicap, IntvEliminatePlayer, IntvToughnessBoost, IntvAdjustMusicTempo} {
			if NeedsFreshReadings(iv) {
				t.Errorf("NeedsFreshReadings(%s) = true", iv)
			}
		}
	})
}
