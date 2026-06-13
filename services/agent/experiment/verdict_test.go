package experiment

import (
	"math"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// armFrom builds an ArmStat by folding the given samples through the real Welford
// recurrence, so Count/Mean/M2 are exactly what the journal would persist. Building
// the Summary from raw samples (rather than hand-setting M2) keeps the tests honest
// about what the verdict actually reads.
func armFrom(samples ...float64) *journal.ArmStat {
	var st journal.ArmStat
	for _, s := range samples {
		st.Add(s)
	}
	return &st
}

// summaryWith assembles a journal.Summary with the canonical experimental/control
// arms from the given samples.
func summaryWith(exp, ctl *journal.ArmStat) journal.Summary {
	return journal.Summary{
		ExperimentID: "exp_test",
		Status:       "running",
		Arms: map[string]*journal.ArmStat{
			ArmExperimental: exp,
			ArmControl:      ctl,
		},
	}
}

// repeat returns a slice of n copies of v, plus a small spread so variance is
// non-zero (otherwise pooled SD is 0 and Cohen's d is undefined). The spread is
// symmetric so the mean is exactly v.
func spread(n int, mean, halfWidth float64) []float64 {
	out := make([]float64, 0, n)
	for i := 0; i < n; i++ {
		if i%2 == 0 {
			out = append(out, mean+halfWidth)
		} else {
			out = append(out, mean-halfWidth)
		}
	}
	return out
}

func TestVerdict_UnderPowered_Inconclusive(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Experimental arm has only 3 samples (< min-N 8) but a HUGE delta vs control.
	// Must still be INCONCLUSIVE — under-powered, never a false-positive promote.
	exp := armFrom(spread(3, 100.0, 1.0)...)
	ctl := armFrom(spread(20, 0.0, 1.0)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true (both arms present), got false")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("under-powered verdict = %q, want %q", got.Outcome, OutcomeInconclusive)
	}
	if got.Significant {
		t.Fatalf("under-powered verdict should not be significant")
	}
}

func TestVerdict_ClearWin_Promote(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Both arms meet N; experimental mean far above control with tight spread →
	// large positive Cohen's d → PROMOTE.
	exp := armFrom(spread(10, 0.80, 0.02)...)
	ctl := armFrom(spread(10, 0.50, 0.02)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote {
		t.Fatalf("clear-win verdict = %q (reason %q), want %q", got.Outcome, got.Reason, CohortOutcomePromote)
	}
	if !got.Significant {
		t.Fatalf("clear-win verdict should be significant")
	}
	if got.Delta <= 0 {
		t.Fatalf("clear-win delta = %v, want positive", got.Delta)
	}
}

func TestVerdict_ClearLoss_Discard(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Experimental far BELOW control → large negative effect → DISCARD.
	exp := armFrom(spread(10, 0.30, 0.02)...)
	ctl := armFrom(spread(10, 0.70, 0.02)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomeDiscard {
		t.Fatalf("clear-loss verdict = %q (reason %q), want %q", got.Outcome, got.Reason, CohortOutcomeDiscard)
	}
	if !got.Significant {
		t.Fatalf("clear-loss verdict should be significant")
	}
	if got.Delta >= 0 {
		t.Fatalf("clear-loss delta = %v, want negative", got.Delta)
	}
}

func TestVerdict_Borderline_Inconclusive(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// N met, but the means differ only slightly relative to the spread → small
	// |Cohen's d| below the 0.5 threshold → INCONCLUSIVE (within noise).
	exp := armFrom(spread(12, 0.55, 0.20)...)
	ctl := armFrom(spread(12, 0.50, 0.20)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("borderline verdict = %q (reason %q), want %q", got.Outcome, got.Reason, OutcomeInconclusive)
	}
	if got.Significant {
		t.Fatalf("borderline verdict should not be significant")
	}
}

func TestVerdict_MissingArm_NoVerdict(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Only the experimental arm present — cannot compare; bool must be false so the
	// registry leaves any prior verdict untouched.
	s := journal.Summary{
		ExperimentID: "exp_test",
		Arms:         map[string]*journal.ArmStat{ArmExperimental: armFrom(spread(10, 0.8, 0.02)...)},
	}
	if _, ok := v.Evaluate(s); ok {
		t.Fatalf("missing control arm should yield ok=false")
	}
}

func TestVerdict_NilArmStat_NoVerdict(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	s := journal.Summary{
		Arms: map[string]*journal.ArmStat{
			ArmExperimental: armFrom(spread(10, 0.8, 0.02)...),
			ArmControl:      nil,
		},
	}
	if _, ok := v.Evaluate(s); ok {
		t.Fatalf("nil control ArmStat should yield ok=false")
	}
}

func TestVerdict_EmptyAndSingleSample_Inconclusive(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)

	// Count 0 / Count 1 arms: must not panic and must be INCONCLUSIVE (under-powered
	// AND variance undefined). Both still need ok=true so the interim verdict is
	// recorded.
	cases := []struct {
		name     string
		exp, ctl *journal.ArmStat
	}{
		{"both empty", armFrom(), armFrom()},
		{"exp single, ctl empty", armFrom(0.9), armFrom()},
		{"both single", armFrom(0.9), armFrom(0.1)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := v.Evaluate(summaryWith(tc.exp, tc.ctl))
			if !ok {
				t.Fatalf("expected ok=true (arms present)")
			}
			if got.Outcome != OutcomeInconclusive {
				t.Fatalf("verdict = %q, want %q", got.Outcome, OutcomeInconclusive)
			}
		})
	}
}

func TestVerdict_ZeroVariance_Inconclusive(t *testing.T) {
	v := NewVerdict(2, 0.5, nil)
	// Both arms meet (lowered) min-N but every sample is identical → pooled SD 0 →
	// effect undefined → INCONCLUSIVE, no panic / no divide-by-zero.
	exp := armFrom(0.5, 0.5, 0.5, 0.5)
	ctl := armFrom(0.5, 0.5, 0.5, 0.5)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("zero-variance verdict = %q (reason %q), want %q", got.Outcome, got.Reason, OutcomeInconclusive)
	}
}

// TestVerdict_UsesUnbiasedVariance constructs a case where the population variance
// (M2/n) clears the effect threshold but the UNBIASED sample variance (M2/(n−1))
// does not — proving the gate uses the sample (n−1) estimator, not population.
//
// With small N the unbiased variance is larger (divides by n−1 < n), so the pooled
// SD is larger and Cohen's d is SMALLER. We pick a delta that lands ABOVE 0.5 under
// population variance but BELOW 0.5 under sample variance → the correct (unbiased)
// implementation returns INCONCLUSIVE; a population-variance bug would return
// PROMOTE.
func TestVerdict_UsesUnbiasedVariance(t *testing.T) {
	v := NewVerdict(2, 0.5, nil)

	// n=4 per arm. Spread halfWidth h gives M2 = 4*h^2 (4 deviations of ±h).
	// population var = M2/4 = h^2 ;  sample var = M2/3 = (4/3)h^2.
	// Pick h and delta so d_pop >= 0.5 > d_sample.
	const h = 0.10
	// population pooled SD = h = 0.10 ;  sample pooled SD = sqrt(4/3)*h ≈ 0.1155.
	// delta = 0.054: d_pop = 0.54 (>=0.5 → promote);  d_sample ≈ 0.467 (<0.5 → inconclusive).
	exp := armFrom(spread(4, 0.554, h)...)
	ctl := armFrom(spread(4, 0.500, h)...)

	// Sanity: confirm the population-variance effect WOULD have promoted, so the
	// test is actually discriminating between the two estimators.
	popVarExp := exp.M2 / float64(exp.Count)
	popVarCtl := ctl.M2 / float64(ctl.Count)
	popPooled := math.Sqrt((popVarExp + popVarCtl) / 2)
	dPop := (exp.Mean - ctl.Mean) / popPooled
	if dPop < 0.5 {
		t.Fatalf("test misconfigured: population-variance d=%.4f should be >= 0.5", dPop)
	}

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("unbiased-variance verdict = %q (population d=%.4f), want %q — "+
			"the gate must use sample (n-1) variance, not population", got.Outcome, dPop, OutcomeInconclusive)
	}
}

func TestVerdict_DefaultsOnNonPositiveConfig(t *testing.T) {
	v := NewVerdict(0, -1, nil)
	if v.minN != DefaultMinNPerArm {
		t.Fatalf("minN = %d, want default %d", v.minN, DefaultMinNPerArm)
	}
	if v.effectThreshold != DefaultEffectThreshold {
		t.Fatalf("effectThreshold = %v, want default %v", v.effectThreshold, DefaultEffectThreshold)
	}
	if v.stat == nil {
		t.Fatalf("nil stat should fall back to the default gate")
	}
}

func TestVerdictFromEnv(t *testing.T) {
	t.Setenv(minNEnv, "5")
	t.Setenv(effectEnv, "0.8")
	v := NewVerdictFromEnv()
	if v.minN != 5 {
		t.Fatalf("env minN = %d, want 5", v.minN)
	}
	if v.effectThreshold != 0.8 {
		t.Fatalf("env effectThreshold = %v, want 0.8", v.effectThreshold)
	}

	// Invalid / empty values fall back to defaults.
	t.Setenv(minNEnv, "not-a-number")
	t.Setenv(effectEnv, "")
	v2 := NewVerdictFromEnv()
	if v2.minN != DefaultMinNPerArm || v2.effectThreshold != DefaultEffectThreshold {
		t.Fatalf("invalid env should fall back to defaults, got minN=%d threshold=%v", v2.minN, v2.effectThreshold)
	}
}

// TestVerdict_SwappableStrategy verifies a custom CohortStat replaces the gate
// without touching the seam — the design's swappability requirement (§8.3).
func TestVerdict_SwappableStrategy(t *testing.T) {
	// A stub strategy that always reports a large positive effect, ignoring the
	// actual stats — stands in for a future Welch / non-parametric test.
	v := NewVerdict(2, 0.5, constantStat{effect: 5.0, ok: true})
	exp := armFrom(0.5, 0.5, 0.5) // identical samples — default gate would be inconclusive
	ctl := armFrom(0.5, 0.5, 0.5)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote {
		t.Fatalf("custom strategy verdict = %q, want %q (strategy not honored)", got.Outcome, CohortOutcomePromote)
	}
}

type constantStat struct {
	effect float64
	ok     bool
}

func (c constantStat) Effect(_, _ journal.Welford) (float64, bool) { return c.effect, c.ok }
