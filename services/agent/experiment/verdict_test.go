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
	// minN / minPairs stay env-derived (the registry's bootstrap default the live
	// verdict_min_n / verdict_min_pairs flags fall back to).
	t.Setenv(minNEnv, "5")
	v := NewVerdictFromEnv()
	if v.minN != 5 {
		t.Fatalf("env minN = %d, want 5", v.minN)
	}
	// The four migrated float knobs (#1214) are NO LONGER read from env — they are LIVE
	// agent.json flags. NewVerdictFromEnv seeds them from the Default* constants (the
	// fail-open fallback); the live flag value arrives later via SetFloatThresholds.
	if v.effectThreshold != DefaultEffectThreshold {
		t.Fatalf("effectThreshold = %v, want default %v (no longer env-read)", v.effectThreshold, DefaultEffectThreshold)
	}

	// Invalid / empty values fall back to defaults.
	t.Setenv(minNEnv, "not-a-number")
	v2 := NewVerdictFromEnv()
	if v2.minN != DefaultMinNPerArm || v2.effectThreshold != DefaultEffectThreshold {
		t.Fatalf("invalid env should fall back to defaults, got minN=%d threshold=%v", v2.minN, v2.effectThreshold)
	}
}

// TestVerdictFromEnv_Floors (#1214) — the practical-significance floor, SD floor,
// effect threshold and anchor margin are NO LONGER env-read: NewVerdictFromEnv seeds
// them from the Default* constants (the flags' fail-open fallback). This asserts the
// construction defaults; the live-flag override is covered by TestVerdict_SetFloatThresholds.
func TestVerdictFromEnv_Floors(t *testing.T) {
	// Even with the legacy env vars set, the four floats default to the code constants
	// (env is ignored now — the values come from flags via SetFloatThresholds).
	t.Setenv("AGENT_VERDICT_MIN_RAW_EFFECT", "0.05")
	t.Setenv("AGENT_VERDICT_SD_FLOOR", "0.001")
	v := NewVerdictFromEnv()
	if v.minRawEffect != DefaultMinRawEffect {
		t.Fatalf("minRawEffect = %v, want default %v (env no longer read, #1214)", v.minRawEffect, DefaultMinRawEffect)
	}
	if v.sdFloor != DefaultSDFloor {
		t.Fatalf("sdFloor = %v, want default %v (env no longer read, #1214)", v.sdFloor, DefaultSDFloor)
	}
	// The default gate is built WITH the default sdFloor so the two-arm path is floored.
	g, ok := v.stat.(effectSizeGate)
	if !ok {
		t.Fatalf("default stat should be effectSizeGate, got %T", v.stat)
	}
	if g.sdFloor != DefaultSDFloor {
		t.Fatalf("two-arm gate sdFloor = %v, want default %v", g.sdFloor, DefaultSDFloor)
	}
}

// TestVerdict_SetFloatThresholds (#1214) is the live-tuning acceptance for the four
// migrated float verdict knobs: SetFloatThresholds updates effectThreshold /
// minRawEffect / sdFloor / anchorMargin (and keeps the two-arm gate's sdFloor in sync),
// while a non-positive value per knob is IGNORED (keeps the current value) — the same
// fail-safe floor SetThresholds uses for min_n/min_pairs.
func TestVerdict_SetFloatThresholds(t *testing.T) {
	v := NewVerdictFromEnv()

	v.SetFloatThresholds(0.8, 0.05, 0.001, 0.03)
	if v.effectThreshold != 0.8 {
		t.Fatalf("effectThreshold = %v, want 0.8 after SetFloatThresholds", v.effectThreshold)
	}
	if v.minRawEffect != 0.05 {
		t.Fatalf("minRawEffect = %v, want 0.05 after SetFloatThresholds", v.minRawEffect)
	}
	if v.sdFloor != 0.001 {
		t.Fatalf("sdFloor = %v, want 0.001 after SetFloatThresholds", v.sdFloor)
	}
	if v.anchorMargin != 0.03 {
		t.Fatalf("anchorMargin = %v, want 0.03 after SetFloatThresholds", v.anchorMargin)
	}
	// The two-arm gate's floor must track the live verdict_sd_floor value.
	if g, ok := v.stat.(effectSizeGate); !ok || g.sdFloor != 0.001 {
		t.Fatalf("two-arm gate sdFloor not synced to live value: %+v (ok=%v)", v.stat, ok)
	}

	// Non-positive values per knob are ignored (keep the current value).
	v.SetFloatThresholds(0, -1, 0, -2)
	if v.effectThreshold != 0.8 || v.minRawEffect != 0.05 || v.sdFloor != 0.001 || v.anchorMargin != 0.03 {
		t.Fatalf("non-positive SetFloatThresholds must keep current values, got eff=%v raw=%v sd=%v anchor=%v",
			v.effectThreshold, v.minRawEffect, v.sdFloor, v.anchorMargin)
	}
}

// TestVerdict_SetFloatThresholds_DrivesVerdict (#1214) proves the live float knob
// actually drives the verdict outcome: a borderline standardized effect that PROMOTEs
// under the default effect threshold flips to INCONCLUSIVE once SetFloatThresholds
// raises verdict_effect_threshold above it — the flag value drives the gate.
func TestVerdict_SetFloatThresholds_DrivesVerdict(t *testing.T) {
	v := NewVerdict(8, DefaultEffectThreshold, nil)
	// Arms with a clear, practically-significant separation: a medium-ish Cohen's d that
	// clears the 0.5 default threshold (promote) but not a raised 2.0 threshold.
	exp := armFrom(spread(12, 0.70, 0.10)...)
	ctl := armFrom(spread(12, 0.50, 0.10)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok || got.Outcome != CohortOutcomePromote {
		t.Fatalf("with default threshold expected PROMOTE, got outcome=%v ok=%v reason=%q", got.Outcome, ok, got.Reason)
	}

	// Raise the live effect threshold above the observed effect: the SAME data now reads
	// as within-noise ⇒ INCONCLUSIVE. The flag value drove the verdict.
	v.SetFloatThresholds(2.0, 0, 0, 0)
	got2, ok2 := v.Evaluate(summaryWith(exp, ctl))
	if !ok2 || got2.Outcome != OutcomeInconclusive {
		t.Fatalf("after raising effect threshold expected INCONCLUSIVE, got outcome=%v ok=%v reason=%q", got2.Outcome, ok2, got2.Reason)
	}
}

// TestVerdict_TwoArm_TinyDelta_NotPractical (#1042) is the two-arm analog of the
// dry-run bug: N met, a large standardized Cohen's d (tight spread), but a raw mean
// delta FAR below the practical-significance floor ⇒ INCONCLUSIVE, not promote.
func TestVerdict_TwoArm_TinyDelta_NotPractical(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Means differ by only 0.005 (< 0.02 floor) but the spread is tiny (±0.0005), so
	// Cohen's d is huge — exactly the trap the floor must catch.
	exp := armFrom(spread(12, 0.5050, 0.0005)...)
	ctl := armFrom(spread(12, 0.5000, 0.0005)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("two-arm tiny-delta verdict = %+v, want INCONCLUSIVE (raw-effect floor)", got)
	}
}

// TestVerdict_TwoArm_MeaningfulDelta_TightSpread_Promote (#1042): a delta ABOVE the
// raw-effect floor with a clear direction still promotes — the floor ADDS to, doesn't
// replace, the effect-size test.
func TestVerdict_TwoArm_MeaningfulDelta_TightSpread_Promote(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	// Delta 0.10 (>> 0.02 floor), tight spread ⇒ large Cohen's d ⇒ PROMOTE.
	exp := armFrom(spread(12, 0.60, 0.01)...)
	ctl := armFrom(spread(12, 0.50, 0.01)...)

	got, ok := v.Evaluate(summaryWith(exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("meaningful-delta verdict = %+v, want PROMOTE", got)
	}
}

// TestVerdict_TwoArm_SDFloor_CapsCohenD (#1042): with the SD floor active, even a
// pooled SD near 0 yields a finite Cohen's d (no Inf/NaN). The default gate inherits
// DefaultSDFloor.
func TestVerdict_TwoArm_SDFloor_CapsCohenD(t *testing.T) {
	g := effectSizeGate{sdFloor: DefaultSDFloor}
	// Near-zero spread, meaningful mean gap: without a floor Cohen's d would blow up.
	exp := journal.Welford{}
	ctl := journal.Welford{}
	for _, s := range spread(10, 0.50, 1e-12) {
		exp.Add(s)
	}
	for _, s := range spread(10, 0.40, 1e-12) {
		ctl.Add(s)
	}
	effect, ok := g.Effect(exp, ctl)
	if !ok {
		t.Fatalf("expected ok=true with SD floor active")
	}
	if math.IsInf(effect, 0) || math.IsNaN(effect) {
		t.Fatalf("Cohen's d = %v, want finite (SD floor must cap it)", effect)
	}
	// delta 0.10 / sdFloor 1e-4 = 1000 — large but finite.
	if effect < 100 {
		t.Fatalf("expected a large (floored) effect, got %v", effect)
	}
}

// TestVerdict_DefaultsIncludeFloors (#1042): the default constructor wires the floors.
func TestVerdict_DefaultsIncludeFloors(t *testing.T) {
	v := NewVerdict(8, 0.5, nil)
	if v.minRawEffect != DefaultMinRawEffect {
		t.Fatalf("minRawEffect = %v, want default %v", v.minRawEffect, DefaultMinRawEffect)
	}
	if v.sdFloor != DefaultSDFloor {
		t.Fatalf("sdFloor = %v, want default %v", v.sdFloor, DefaultSDFloor)
	}
}

// TestVerdict_SwappableStrategy verifies a custom CohortStat replaces the gate
// without touching the seam — the design's swappability requirement (§8.3).
func TestVerdict_SwappableStrategy(t *testing.T) {
	// A stub strategy that always reports a large positive effect, ignoring the
	// actual stats — stands in for a future Welch / non-parametric test.
	v := NewVerdict(2, 0.5, constantStat{effect: 5.0, ok: true})
	// The arms carry a clear raw mean difference (0.8 vs 0.5) so the #1042 practical-
	// significance floor passes; the point is that the SWAPPABLE strategy's reported
	// effect (5.0) — not the default Cohen's d — drives the promote.
	exp := armFrom(0.8, 0.8, 0.8)
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

// TestVerdict_SetThresholds_Live is the #1044 live-tunable-gate AC: SetThresholds
// retunes the min-N / min-pairs gates with no reconstruction (a hot-reload of
// verdict_min_n / verdict_min_pairs), and a non-positive value is IGNORED (keeps the
// current gate — the fail-safe floor that never loosens the verdict on a flagd hiccup).
func TestVerdict_SetThresholds_Live(t *testing.T) {
	v := NewVerdict(4, 0.2, nil) // start at min-N 4
	// 8 samples/arm with a clear positive effect: conclusive at min-N 4.
	s := summaryWith(armFrom(spread(8, 0.9, 0.02)...), armFrom(spread(8, 0.1, 0.02)...))
	if got, _ := v.Evaluate(s); !got.Significant {
		t.Fatalf("min-N 4 with 8/arm + large effect should be conclusive, got %+v", got)
	}

	// Tighten min-N to 16 live: the SAME 8/arm summary is now under-powered.
	v.SetThresholds(16, 16)
	got, ok := v.Evaluate(s)
	if !ok {
		t.Fatal("Evaluate ok=false for a populated summary")
	}
	if got.Significant {
		t.Fatalf("after SetThresholds(16) the 8/arm summary must be inconclusive (under-powered), got %+v", got)
	}

	// A non-positive update is ignored (min-N stays 16): still under-powered.
	v.SetThresholds(0, -1)
	if got, _ := v.Evaluate(s); got.Significant {
		t.Fatalf("non-positive SetThresholds loosened the gate; want still inconclusive, got %+v", got)
	}

	// Loosen back to min-N 4 live: conclusive again.
	v.SetThresholds(4, 3)
	if got, _ := v.Evaluate(s); !got.Significant {
		t.Fatalf("after SetThresholds(4) the 8/arm summary should be conclusive again, got %+v", got)
	}
}
