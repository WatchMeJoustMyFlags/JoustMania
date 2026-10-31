package characteristics

import (
	"math"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

var t0 = time.Unix(1_000, 0)

func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

// history returns one sample per second in (from, from+n] at intensity.
func history(from time.Time, n int, intensity float64) []gamecontext.MovementSample {
	out := make([]gamecontext.MovementSample, n)
	for i := range out {
		out[i] = gamecontext.MovementSample{At: from.Add(time.Duration(i+1) * time.Second), Intensity: intensity}
	}
	return out
}

func ctxWith(players map[string][]gamecontext.MovementSample) gamecontext.GameContext {
	c := gamecontext.GameContext{SessionID: "g", Players: map[string]*gamecontext.PlayerSignals{}}
	c.Session.DeathThreshold = fp(2)
	for s, h := range players {
		c.Players[s] = &gamecontext.PlayerSignals{Serial: s, Active: bp(true), History: h}
	}
	return c
}

// UC: UC-040 A44 BR-034
func TestActivity(t *testing.T) {
	w := Window{From: t0, To: t0.Add(10 * time.Second)}
	t.Run("UC-040/A44 activity is intensity over the death threshold", func(t *testing.T) {
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": history(t0, 10, 1), "B": history(t0, 10, 0.5)})
		if v, ok := PlayerActivity(c, "A", w); !ok || v != 0.5 {
			t.Fatalf("A = %v %v, want 0.5", v, ok)
		}
		if v, ok := FieldActivity(c, w); !ok || v != 0.375 {
			t.Fatalf("field = %v %v, want 0.375", v, ok)
		}
		if v, ok := Closeness(c, w); !ok || v != 0.25 {
			t.Fatalf("closeness = %v %v, want 0.25", v, ok)
		}
	})
	t.Run("UC-040/A44 the threshold in force at each sample", func(t *testing.T) {
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": history(t0, 10, 1)})
		c.SpeedTrack = []gamecontext.SpeedSample{{At: t0, DeathThreshold: fp(1)}, {At: t0.Add(5500 * time.Millisecond), DeathThreshold: fp(4)}}
		v, ok := PlayerActivity(c, "A", w)
		if !ok || math.Abs(v-(5*1.0+5*0.25)/10) > 1e-9 {
			t.Fatalf("A = %v %v", v, ok)
		}
	})
	t.Run("UC-040/A44 below half coverage is data missing", func(t *testing.T) {
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": history(t0, 4, 1), "B": history(t0, 10, 1)})
		if _, ok := PlayerActivity(c, "A", w); ok {
			t.Fatal("4 of 10 buckets must be data missing")
		}
		if _, ok := FieldActivity(c, w); ok {
			t.Fatal("one covered player is not a field")
		}
	})
	t.Run("UC-040/A44 no threshold is data missing", func(t *testing.T) {
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": history(t0, 10, 1)})
		c.Session.DeathThreshold = nil
		if _, ok := PlayerActivity(c, "A", w); ok {
			t.Fatal("no threshold must be data missing")
		}
	})
	t.Run("UC-040/A44 eliminated players leave the field", func(t *testing.T) {
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": history(t0, 10, 1), "B": history(t0, 10, 0.5), "C": history(t0, 10, 2)})
		c.Session.EliminationSequence = []string{"C"}
		if v, ok := FieldActivity(c, w); !ok || v != 0.375 {
			t.Fatalf("field = %v %v, want 0.375 without C", v, ok)
		}
	})
	t.Run("UC-040/A44 change is the swing of 5 s field means", func(t *testing.T) {
		h := append(history(t0, 5, 0.5), history(t0.Add(5*time.Second), 5, 1.5)...)
		c := ctxWith(map[string][]gamecontext.MovementSample{"A": h, "B": h})
		if v, ok := Change(c, w); !ok || v != 0.5 {
			t.Fatalf("change = %v %v, want 0.5", v, ok)
		}
	})
}

// UC: UC-040 A44 BR-034
func TestEliminations(t *testing.T) {
	c := gamecontext.GameContext{}
	c.Session.EliminationSequence = []string{"A", "B"}
	c.Timeline = []gamecontext.TimelineEvent{
		{At: t0.Add(2 * time.Second), Kind: gamecontext.EventElimination, Serial: "A"},
		{At: t0.Add(12 * time.Second), Kind: gamecontext.EventElimination, Serial: "B"},
	}
	w := Window{From: t0, To: t0.Add(10 * time.Second)}
	if n, complete := Eliminations(c, w); n != 1 || !complete {
		t.Fatalf("n = %d complete = %v, want 1 true", n, complete)
	}
	if v, ok := EliminationPace(c, w); !ok || v != 6 {
		t.Fatalf("pace = %v, want 6/min", v)
	}
	if at, ok := EliminatedAt(c, "B"); !ok || !at.Equal(t0.Add(12*time.Second)) {
		t.Fatalf("B eliminated at %v %v", at, ok)
	}
	empty := gamecontext.GameContext{}
	empty.Session.EliminationSequence = []string{"A"}
	if _, complete := Eliminations(empty, w); complete {
		t.Fatal("an empty timeline with eliminations is incomplete")
	}
}

// UC: UC-040 A44 BR-034
// TestActivity_CoverageCountsSeconds: several samples in one second count once.
func TestActivity_CoverageCountsSeconds(t *testing.T) {
	var h []gamecontext.MovementSample
	for i := 0; i < 12; i++ {
		h = append(h, gamecontext.MovementSample{At: t0.Add(time.Second + time.Duration(i)*200*time.Millisecond), Intensity: 1})
	}
	c := ctxWith(map[string][]gamecontext.MovementSample{"A": h})
	if _, ok := PlayerActivity(c, "A", Window{From: t0, To: t0.Add(10 * time.Second)}); ok {
		t.Fatal("12 samples in 3 seconds of a 10 s window must be data missing")
	}
}
