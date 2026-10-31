package decide

import (
	"math"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// track builds a one-reading-per-second track whose newest reading is at newest.
func track(newest time.Time, intensities ...float64) []gamecontext.MovementSample {
	out := make([]gamecontext.MovementSample, len(intensities))
	for i, v := range intensities {
		out[i] = gamecontext.MovementSample{At: newest.Add(-time.Duration(len(intensities)-1-i) * time.Second), Intensity: v}
	}
	return out
}

// UC: UC-040 A43 BR-032
func TestSustainedDanger(t *testing.T) {
	now := time.Unix(1_000, 0)
	thr := fptr(2.0)
	for _, tc := range []struct {
		name      string
		samples   []gamecontext.MovementSample
		speed     []gamecontext.SpeedSample
		threshold *float64
		level     float64
		n         int
		want      bool
		wantMin   float64
	}{
		{name: "three readings at or above 0.85: sustained", samples: track(now, 1.7, 1.9, 2.2), threshold: thr, level: 0.85, n: 3, want: true, wantMin: 0.85},
		{name: "only the newest readings count", samples: track(now, 0.2, 1.8, 1.9, 2.0), threshold: thr, level: 0.85, n: 3, want: true, wantMin: 0.9},
		{name: "a single spike", samples: track(now, 1.0, 1.0, 2.4), threshold: thr, level: 0.85, n: 3},
		{name: "a dip below the level", samples: track(now, 1.8, 1.69, 1.8), threshold: thr, level: 0.85, n: 3},
		{name: "the same reading in three buckets counts once", samples: track(now, 1.9, 1.9, 1.9), threshold: thr, level: 0.85, n: 3},
		{name: "a repeated reading counts once, the window goes on", samples: track(now, 1.8, 1.9, 1.9, 2.0), threshold: thr, level: 0.85, n: 3, want: true, wantMin: 0.9},
		{name: "a frozen gauge: the newest value repeated with fresh times is as old as its first reading",
			samples: track(now, 1.8, 1.9, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0), threshold: thr, level: 0.85, n: 3},
		{name: "a frozen value still within 3 s counts once and the window goes on",
			samples: track(now, 1.8, 1.9, 2.0, 2.0, 2.0), threshold: thr, level: 0.85, n: 3, want: true, wantMin: 0.9},
		{name: "a window longer than n x 2.5 s in total", samples: []gamecontext.MovementSample{
			{At: now.Add(-7600 * time.Millisecond), Intensity: 1.8}, {At: now.Add(-5200 * time.Millisecond), Intensity: 1.85},
			{At: now.Add(-2800 * time.Millisecond), Intensity: 1.85}, {At: now.Add(-400 * time.Millisecond), Intensity: 1.85},
			{At: now, Intensity: 2.0},
		}, threshold: thr, level: 0.85, n: 3},
		{name: "fewer readings than n", samples: track(now, 1.9, 2.0), threshold: thr, level: 0.85, n: 3},
		{name: "a stale newest reading", samples: track(now.Add(-DangerSampleStaleAfter-time.Millisecond), 1.8, 1.9, 2.0), threshold: thr, level: 0.85, n: 3},
		{name: "the newest reading exactly at the stale limit", samples: track(now.Add(-DangerSampleStaleAfter), 1.8, 1.9, 2.0), threshold: thr, level: 0.85, n: 3, want: true, wantMin: 0.9},
		{name: "a reading after the context time", samples: track(now.Add(time.Second), 1.8, 1.9, 2.0), threshold: thr, level: 0.85, n: 3},
		{name: "a gap longer than 2.5 s", samples: []gamecontext.MovementSample{
			{At: now.Add(-5 * time.Second), Intensity: 1.8}, {At: now.Add(-time.Second), Intensity: 1.9}, {At: now, Intensity: 2.0},
		}, threshold: thr, level: 0.85, n: 3},
		{name: "no threshold", samples: track(now, 1.8, 1.9, 2.0), level: 0.85, n: 3},
		{name: "a zero threshold", samples: track(now, 1.8, 1.9, 2.0), threshold: fptr(0), level: 0.85, n: 3},
		{name: "an infinite reading", samples: track(now, 1.8, math.Inf(1), 2.0), threshold: thr, level: 0.85, n: 3},
		{name: "n zero", samples: track(now, 1.8, 1.9, 2.0), threshold: thr, level: 0.85, n: 0},
		{name: "a NaN level", samples: track(now, 1.8, 1.9, 2.0), threshold: thr, level: math.NaN(), n: 3},
		{name: "no readings", threshold: thr, level: 0.85, n: 3},
		{name: "each reading against the threshold in force then: a raised threshold later does not count against earlier readings",
			samples: track(now, 1.8, 1.9, 2.6),
			speed: []gamecontext.SpeedSample{
				{At: now.Add(-10 * time.Second), DeathThreshold: fptr(2.0)},
				{At: now.Add(-500 * time.Millisecond), DeathThreshold: fptr(3.0)},
			}, threshold: fptr(3.0), level: 0.85, n: 3, want: true, wantMin: 2.6 / 3.0},
	} {
		t.Run("UC-040/A43 BR-032 "+tc.name, func(t *testing.T) {
			gotMin, ok := SustainedDanger(tc.samples, tc.speed, tc.threshold, now, tc.level, tc.n)
			if ok != tc.want {
				t.Fatalf("ok = %v, want %v", ok, tc.want)
			}
			if ok && math.Abs(gotMin-tc.wantMin) > 1e-9 {
				t.Fatalf("minRatio = %v, want %v", gotMin, tc.wantMin)
			}
		})
	}
}
