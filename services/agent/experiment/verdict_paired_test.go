package experiment

import (
	"math"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// verdict_paired_test.go covers the Common-Random-Numbers paired-difference verdict
// path (#1004): d_z = mean(d)/sd(d) over Summary.PairDiff, the min-pairs gate, the
// fallback to the two-arm Cohen's d when no pairs exist, and the CRN precision
// gain over the two-arm path on the same data.

// pairDiffFrom folds the given per-pair differences d_i through the real Welford
// recurrence, producing the PairDiff accumulator the journal would persist.
func pairDiffFrom(diffs ...float64) *journal.Welford {
	w := &journal.Welford{}
	for _, d := range diffs {
		w.Add(d)
	}
	return w
}

// pairedSummary assembles a Summary carrying ONLY a PairDiff (the canonical arms are
// left empty so a regression to the two-arm path would be visible as ok=false).
func pairedSummary(pd *journal.Welford) journal.Summary {
	return journal.Summary{
		ExperimentID: "exp_paired",
		Status:       "running",
		Arms:         map[string]*journal.ArmStat{},
		PairDiff:     pd,
	}
}

func TestPairedVerdict_PromotesOnPositiveDz(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	// 6 pairs, mean(d)=0.30, small spread ⇒ large d_z ⇒ PROMOTE.
	pd := pairDiffFrom(0.28, 0.32, 0.29, 0.31, 0.30, 0.30)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE significant", got)
	}
	if math.Abs(got.Delta-pd.Mean) > 1e-9 {
		t.Errorf("Delta = %v, want mean(d)=%v", got.Delta, pd.Mean)
	}
}

func TestPairedVerdict_DiscardsOnNegativeDz(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	pd := pairDiffFrom(-0.28, -0.32, -0.29, -0.31, -0.30, -0.30)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomeDiscard || !got.Significant {
		t.Fatalf("verdict = %+v, want DISCARD significant", got)
	}
}

func TestPairedVerdict_MinPairsGate(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	// Only 3 pairs (< min-pairs 5), even with a huge clean effect ⇒ INCONCLUSIVE.
	pd := pairDiffFrom(0.50, 0.50, 0.50)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("verdict = %+v, want under-powered INCONCLUSIVE", got)
	}
}

func TestPairedVerdict_NoOpFlagWithinNoise(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	// A no-op flag: per-pair deltas tightly centered on 0 (CRN cancels shared
	// variance). |d_z| is small ⇒ within-noise INCONCLUSIVE (not a false positive).
	pd := pairDiffFrom(0.01, -0.01, 0.02, -0.02, 0.00, 0.01, -0.01)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("verdict = %+v, want within-noise INCONCLUSIVE", got)
	}
}

// TestPairedVerdict_ZeroSpreadMeaningfulDelta_SDFloor (#1042): every pair identical
// (zero spread) but with a MEANINGFUL per-pair delta (0.20, well above the raw-effect
// floor). The minimum-SD floor caps the denominator so d_z is large-but-finite (not
// NaN/Inf), and because the raw effect clears the practical floor this is a legitimate
// PROMOTE — the SD floor turns the old "undefined ⇒ inconclusive" bail-out into a sane
// conclusion when the delta actually matters.
func TestPairedVerdict_ZeroSpreadMeaningfulDelta_SDFloor(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	pd := pairDiffFrom(0.20, 0.20, 0.20, 0.20, 0.20, 0.20)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE (meaningful delta, SD floored)", got)
	}
}

// TestPairedVerdict_TinyConsistentDelta_NotPractical (#1042) is the EXACT dry-run
// scenario: a tiny per-pair delta (~ -0.0005) that is perfectly consistent across
// pairs. The SD collapses toward 0 so the standardized d_z is enormous (would be the
// dry-run's d_z=-34), but |mean_d| is FAR below the practical-significance floor →
// INCONCLUSIVE, NOT discard. This is the core bug fix.
func TestPairedVerdict_TinyConsistentDelta_NotPractical(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	// exp ~0.0496..0.0506 vs ctl ~0.0500..0.0510 ⇒ d_i ≈ -0.0004..-0.0006, tiny + consistent.
	pd := pairDiffFrom(-0.0004, -0.0005, -0.0006, -0.0005, -0.0004, -0.0005)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("verdict = %+v, want INCONCLUSIVE (tiny delta below raw-effect floor); the dry-run bug would DISCARD here", got)
	}
}

// TestPairedVerdict_NoOpFlag_ZeroDelta_Inconclusive (#1042): a true no-op flag with
// IDENTICAL fitness per pair ⇒ mean_d == 0 and zero spread. SD floored, but mean_d=0
// is below the raw-effect floor ⇒ INCONCLUSIVE (never a false promote/discard).
func TestPairedVerdict_NoOpFlag_ZeroDelta_Inconclusive(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	pd := pairDiffFrom(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
	got, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("verdict = %+v, want no-op INCONCLUSIVE", got)
	}
}

// TestPairedVerdict_FallbackToTwoArmWhenNoPairs: a Summary with NO PairDiff falls
// back to the two-arm Cohen's d path unchanged (legacy/unpaired experiments).
func TestPairedVerdict_FallbackToTwoArmWhenNoPairs(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	// No PairDiff; canonical arms with a large clean effect ⇒ the two-arm path promotes.
	exp := armFrom(spread(10, 1.0, 0.05)...)
	ctl := armFrom(spread(10, 0.0, 0.05)...)
	s := summaryWith(exp, ctl) // no PairDiff set
	got, ok := v.Evaluate(s)
	if !ok {
		t.Fatalf("expected ok=true via two-arm fallback")
	}
	if got.Outcome != CohortOutcomePromote {
		t.Fatalf("two-arm fallback verdict = %+v, want PROMOTE", got)
	}
}

// TestPairedVerdict_EmptyPairDiffFallsBack: a non-nil but EMPTY PairDiff (Count 0)
// must not take the paired path; it falls back to the two-arm path.
func TestPairedVerdict_EmptyPairDiffFallsBack(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)
	exp := armFrom(spread(10, 1.0, 0.05)...)
	ctl := armFrom(spread(10, 0.0, 0.05)...)
	s := summaryWith(exp, ctl)
	s.PairDiff = &journal.Welford{} // Count 0
	got, ok := v.Evaluate(s)
	if !ok || got.Outcome != CohortOutcomePromote {
		t.Fatalf("empty PairDiff should fall back to two-arm PROMOTE, got %+v ok=%v", got, ok)
	}
}

// TestPairedVerdict_CRNPrecisionBeatsTwoArm is the CRN-sanity check: on the SAME
// underlying games, the paired path reaches a conclusive verdict where the two-arm
// pooled Cohen's d stays inconclusive. We model a per-game shared component (the
// CRN-controlled common variance) plus a small consistent treatment effect: the
// two arms have large overlapping spread (so pooled d is small), but each per-pair
// difference is nearly constant (so d_z is large).
func TestPairedVerdict_CRNPrecisionBeatsTwoArm(t *testing.T) {
	v := NewVerdictWithPairs(8, 5, 0.5, nil)

	// Shared per-game component (large), identical within a seed-matched pair.
	shared := []float64{0.10, 0.90, 0.40, 0.70, 0.20, 0.85, 0.55, 0.30, 0.95, 0.15}
	// Small consistent treatment lift with a TINY per-pair jitter (so the paired SD is
	// non-zero and d_z is defined): the effect is consistently positive and tightly
	// clustered, which is exactly what CRN buys.
	jitter := []float64{0.07, 0.09, 0.08, 0.07, 0.09, 0.08, 0.08, 0.07, 0.09, 0.08}

	var exp, ctl journal.ArmStat
	pd := &journal.Welford{}
	for i, s := range shared {
		e := s + jitter[i]
		c := s
		exp.Add(e)
		ctl.Add(c)
		pd.Add(e - c) // per-pair difference ≈ small effect (CRN cancels `shared`)
	}

	// Two-arm path on the same data: the large shared spread dominates the pooled SD,
	// so Cohen's d is well below threshold ⇒ inconclusive.
	twoArm, ok := v.Evaluate(summaryWith(&exp, &ctl))
	if !ok {
		t.Fatalf("two-arm evaluate not ok")
	}
	if twoArm.Significant {
		t.Fatalf("two-arm path should be inconclusive on this noisy data, got %+v", twoArm)
	}

	// Paired path on the SAME games: each d_i = effect with ~zero spread ⇒ huge d_z.
	paired, ok := v.Evaluate(pairedSummary(pd))
	if !ok {
		t.Fatalf("paired evaluate not ok")
	}
	if paired.Outcome != CohortOutcomePromote || !paired.Significant {
		t.Fatalf("paired path should be conclusive PROMOTE on the same data, got %+v", paired)
	}
	t.Logf("CRN precision: two-arm=%q (delta=%.3f) vs paired=%q (mean_d=%.3f) — paired conclusive, two-arm not",
		twoArm.Outcome, twoArm.Delta, paired.Outcome, paired.Delta)
}
