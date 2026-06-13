package journal

import (
	"math"
	"testing"
)

// directStats computes count, mean, and population variance directly (two-pass) so
// the Welford accumulator can be checked against an independent implementation.
func directStats(samples []float64) (int, float64, float64) {
	n := len(samples)
	if n == 0 {
		return 0, 0, 0
	}
	var sum float64
	for _, s := range samples {
		sum += s
	}
	mean := sum / float64(n)
	var ss float64
	for _, s := range samples {
		d := s - mean
		ss += d * d
	}
	return n, mean, ss / float64(n)
}

func TestWelford_MatchesDirectComputation(t *testing.T) {
	cases := [][]float64{
		{0.71, 0.66, 0.83, 0.59, 0.74, 0.61, 0.69, 0.77},
		{1, 2, 3, 4, 5, 6, 7, 8, 9, 10},
		{0.5, 0.5, 0.5, 0.5},          // zero variance
		{42.0},                        // single sample
		{-1.5, 2.25, -3.75, 0.0, 8.1}, // mixed signs incl. a genuine 0.0
	}
	for i, samples := range cases {
		var w Welford
		for _, s := range samples {
			w.Add(s)
		}
		wantN, wantMean, wantVar := directStats(samples)
		if w.Count != wantN {
			t.Errorf("case %d: count = %d, want %d", i, w.Count, wantN)
		}
		if math.Abs(w.Mean-wantMean) > 1e-9 {
			t.Errorf("case %d: mean = %v, want %v", i, w.Mean, wantMean)
		}
		if math.Abs(w.Variance()-wantVar) > 1e-9 {
			t.Errorf("case %d: variance = %v, want %v", i, w.Variance(), wantVar)
		}
	}
}

func TestWelford_EmptyAndSingle(t *testing.T) {
	var w Welford
	if w.Count != 0 || w.Mean != 0 || w.Variance() != 0 || w.StdDev() != 0 {
		t.Errorf("empty accumulator should be all-zero, got %+v var=%v", w, w.Variance())
	}
	w.Add(3.0)
	if w.Count != 1 || w.Mean != 3.0 || w.Variance() != 0 {
		t.Errorf("single sample: count=%d mean=%v var=%v, want 1/3/0", w.Count, w.Mean, w.Variance())
	}
}

func TestWelford_StdDev(t *testing.T) {
	var w Welford
	for _, s := range []float64{2, 4, 4, 4, 5, 5, 7, 9} {
		w.Add(s)
	}
	// Known population stats for this textbook set: mean 5, variance 4, stddev 2.
	if math.Abs(w.Mean-5) > 1e-9 || math.Abs(w.Variance()-4) > 1e-9 || math.Abs(w.StdDev()-2) > 1e-9 {
		t.Errorf("mean=%v var=%v std=%v, want 5/4/2", w.Mean, w.Variance(), w.StdDev())
	}
}
