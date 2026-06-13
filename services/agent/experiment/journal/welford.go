package journal

import "math"

// welford.go implements Welford's online algorithm for running count / mean /
// variance (design §8.2). It is the journal's sufficient-statistics engine: per
// arm we keep only (count, mean, M2), updated O(1) per game-end, so the live
// aggregate is constant-size in N — the raw per-game fitness samples live ONLY in
// the append-only event log on disk (design §9c). This is the load-bearing
// property behind the bounded-LLM-context discipline (§9.2): 50 games and 500
// games fold into the same three numbers.

// Welford accumulates a stream of fitness samples into running statistics. The
// zero value is a valid empty accumulator (count 0). It is NOT safe for
// concurrent mutation; the Journal serializes Append behind its own lock.
type Welford struct {
	// Count is the number of samples folded in so far.
	Count int `json:"count"`
	// Mean is the running mean of the samples (0 when Count == 0).
	Mean float64 `json:"mean"`
	// M2 is the running sum of squared deviations from the mean — Welford's
	// aggregate. Variance is derived from it (see Variance); it is persisted so a
	// rehydrate restores the accumulator exactly without replaying arithmetic that
	// could drift.
	M2 float64 `json:"m2"`
}

// Add folds one sample into the accumulator using Welford's recurrence. O(1) in
// time and memory regardless of how many samples have been added.
func (w *Welford) Add(sample float64) {
	w.Count++
	delta := sample - w.Mean
	w.Mean += delta / float64(w.Count)
	delta2 := sample - w.Mean
	w.M2 += delta * delta2
}

// Variance returns the population variance (M2 / count). It returns 0 for an
// empty accumulator (no samples) and for a single sample (no spread). Population
// rather than sample variance is used because the arm aggregate describes the
// games actually observed, not an inference about a larger population; the
// significance test (#979) reads count/mean/variance and applies its own
// correction if it wants the sample (n-1) form.
func (w *Welford) Variance() float64 {
	if w.Count == 0 {
		return 0
	}
	return w.M2 / float64(w.Count)
}

// StdDev returns the population standard deviation (sqrt of Variance).
func (w *Welford) StdDev() float64 {
	return math.Sqrt(w.Variance())
}
