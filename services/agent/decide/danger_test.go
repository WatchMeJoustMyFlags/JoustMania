package decide

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

func TestClassifyDanger(t *testing.T) {
	thr := fptr(2.0)
	for _, c := range []struct {
		intensity *float64
		threshold *float64
		want      string
	}{
		{fptr(0), thr, DangerSafe},
		{fptr(1.19), thr, DangerSafe},
		{fptr(1.2), thr, DangerElevated}, // 0.6 exactly
		{fptr(1.69), thr, DangerElevated},
		{fptr(1.7), thr, DangerNearThreshold}, // 0.85 exactly
		{fptr(2.5), thr, DangerNearThreshold}, // over the threshold
		{nil, thr, ""},
		{fptr(1), nil, ""},
		{fptr(1), fptr(0), ""},
		{fptr(math.NaN()), thr, ""},
		{fptr(1), fptr(math.Inf(1)), ""},
	} {
		if got := classifyDanger(c.intensity, c.threshold); got != c.want {
			t.Errorf("classifyDanger(%v, %v) = %q, want %q", deref64(c.intensity), deref64(c.threshold), got, c.want)
		}
	}
}

func deref64(v *float64) any {
	if v == nil {
		return nil
	}
	return *v
}

func TestApplyDangerSignals(t *testing.T) {
	players := []PlayerState{
		{Serial: "A", Alive: true, Intensity: fptr(0.6)},
		{Serial: "B", Alive: true, Intensity: fptr(2.2)},
		{Serial: "C", Alive: true, Intensity: fptr(1.4)},
		{Serial: "D", Alive: false, Intensity: fptr(3.0)}, // out: never ranked
		{Serial: "E", Alive: true},                        // unobserved
	}
	mostHeadroom, struggling := applyDangerSignals(players, fptr(2.3))
	if mostHeadroom != "A" || struggling != "B" {
		t.Errorf("mostHeadroom/struggling = %q/%q", mostHeadroom, struggling)
	}
	wantRank := map[string]int{"A": 3, "B": 1, "C": 2, "D": 0, "E": 0}
	wantDanger := map[string]string{"A": DangerSafe, "B": DangerNearThreshold, "C": DangerElevated}
	for _, p := range players {
		if p.IntensityRank != wantRank[p.Serial] || p.Danger != wantDanger[p.Serial] {
			t.Errorf("%s: rank %d danger %q", p.Serial, p.IntensityRank, p.Danger)
		}
	}
	if m := players[1].ThresholdMargin; m == nil || *m != 0.1 {
		t.Errorf("B margin = %v, want 0.1", deref64(m))
	}
	if players[3].ThresholdMargin != nil || players[4].ThresholdMargin != nil {
		t.Error("out / unobserved players carry no margin")
	}
}

func TestApplyDangerSignals_NoHints(t *testing.T) {
	t.Run("ties", func(t *testing.T) {
		ps := []PlayerState{
			{Serial: "A", Alive: true, Intensity: fptr(2.1)},
			{Serial: "B", Alive: true, Intensity: fptr(2.1)},
		}
		mostHeadroom, struggling := applyDangerSignals(ps, fptr(2.3))
		if mostHeadroom != "" || struggling != "" || ps[0].IntensityRank != 1 || ps[1].IntensityRank != 1 {
			t.Errorf("ties: %q/%q ranks %d/%d", mostHeadroom, struggling, ps[0].IntensityRank, ps[1].IntensityRank)
		}
	})
	t.Run("nobody in danger", func(t *testing.T) {
		ps := []PlayerState{{Serial: "A", Alive: true, Intensity: fptr(0.2)}, {Serial: "B", Alive: true, Intensity: fptr(0.5)}}
		if mostHeadroom, struggling := applyDangerSignals(ps, fptr(2.3)); mostHeadroom != "A" || struggling != "" {
			t.Errorf("%q/%q", mostHeadroom, struggling)
		}
	})
	t.Run("unknown threshold", func(t *testing.T) {
		ps := []PlayerState{{Serial: "A", Alive: true, Intensity: fptr(0.2)}, {Serial: "B", Alive: true, Intensity: fptr(2.5)}}
		mostHeadroom, struggling := applyDangerSignals(ps, nil)
		if mostHeadroom != "A" || struggling != "" || ps[1].Danger != "" || ps[1].IntensityRank != 1 {
			t.Errorf("%q/%q %+v", mostHeadroom, struggling, ps[1])
		}
	})
	t.Run("single player", func(t *testing.T) {
		ps := []PlayerState{{Serial: "A", Alive: true, Intensity: fptr(2.2)}}
		if mostHeadroom, struggling := applyDangerSignals(ps, fptr(2.3)); mostHeadroom != "" || struggling != "" || ps[0].Danger != DangerNearThreshold {
			t.Errorf("%q/%q %+v", mostHeadroom, struggling, ps[0])
		}
	})
}

// TestBuildState_DangerSignals: the signals reach the state, the narrative
// (which every LLM backend reads) and the digest.
func TestBuildState_DangerSignals(t *testing.T) {
	c := sampleCtx()
	st := BuildState(c)
	if st.MostHeadroom != "A" || st.Struggling != "" {
		t.Errorf("mostHeadroom/struggling = %q/%q", st.MostHeadroom, st.Struggling)
	}
	for _, want := range []string{
		"death_threshold=2.30 most_headroom=A\n",
		" danger=safe margin=1.90 intensity_rank=2/2\n",
		"  B alive intensity=1.10 skill=0.30 movement=▄ danger=safe margin=1.20 intensity_rank=1/2\n",
	} {
		if !strings.Contains(st.Narrative, want) {
			t.Errorf("narrative missing %q:\n%s", want, st.Narrative)
		}
	}
	c.Players["B"].MovementIntensity = fptr(2.0)
	st2 := BuildState(c)
	if st2.Struggling != "B" || !strings.Contains(st2.Narrative, "struggling=B") || !strings.Contains(st2.Narrative, "danger=near_threshold margin=0.30") {
		t.Errorf("near-threshold B not surfaced:\n%s", st2.Narrative)
	}
	if st2.Digest == st.Digest {
		t.Error("digest must change with the danger signals")
	}
}

// TestBuildState_Shielded: a dispatched shield within ShieldWindow marks the
// player; an older or blocked one does not.
func TestBuildState_Shielded(t *testing.T) {
	c := sampleCtx()
	c.Timeline = append(c.Timeline,
		gamecontext.TimelineEvent{At: c.UpdatedAt.Add(-4 * time.Second), Kind: gamecontext.EventIntervention, Detail: "grant_shield", Serial: "B"},
		gamecontext.TimelineEvent{At: c.UpdatedAt.Add(-20 * time.Second), Kind: gamecontext.EventIntervention, Detail: "grant_shield", Serial: "A"},
		gamecontext.TimelineEvent{At: c.UpdatedAt.Add(-2 * time.Second), Kind: gamecontext.EventIntervention, Detail: "toughness_boost", Serial: "A", Blocked: true},
	)
	st := BuildState(c)
	if st.Players[0].Shielded || !st.Players[1].Shielded {
		t.Errorf("shielded A=%v B=%v, want false/true", st.Players[0].Shielded, st.Players[1].Shielded)
	}
	if !strings.Contains(st.Narrative, "intensity_rank=1/2 shielded\n") {
		t.Errorf("narrative:\n%s", st.Narrative)
	}
	if st.Digest == BuildState(sampleCtx()).Digest {
		t.Error("digest must change")
	}
}
