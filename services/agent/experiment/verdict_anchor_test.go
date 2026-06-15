package experiment

import (
	"strings"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// verdict_anchor_test.go covers the recent-real SECONDARY substrate-validity anchor
// (#992, relates #1017): a PROMOTE verdict must also hold up against recent REAL
// play, the anchor fails OPEN when no real data exists, and it never touches
// discard / inconclusive verdicts.

// fixedBaseline returns a RecentRealBaseline accessor that always reports the given
// mean as AVAILABLE (ok=true) for any objective — the "recent real data exists"
// case.
func fixedBaseline(mean float64) RecentRealBaseline {
	return func(_ string) (float64, bool) { return mean, true }
}

// noBaseline returns a RecentRealBaseline accessor that always reports NO data
// (ok=false) — the mock/shadow-only reality with no real games.
func noBaseline() RecentRealBaseline {
	return func(_ string) (float64, bool) { return 0, false }
}

// summaryWithObjective is summaryWith plus the per-objective view field the anchor
// reads (journal.Summary.Objective).
func summaryWithObjective(objective string, exp, ctl *journal.ArmStat) journal.Summary {
	s := summaryWith(exp, ctl)
	s.Objective = objective
	return s
}

// clearWinArms are a clean two-arm PROMOTE: experimental mean 0.80, control 0.50,
// tight spread ⇒ large positive Cohen's d. Reused across the anchor cases so only
// the baseline varies.
func clearWinArms() (*journal.ArmStat, *journal.ArmStat) {
	return armFrom(spread(10, 0.80, 0.02)...), armFrom(spread(10, 0.50, 0.02)...)
}

func TestVerdict_Anchor_RecentRealAtOrBelowExperimental_PromoteStands(t *testing.T) {
	// Recent-real baseline 0.50 < experimental 0.80 ⇒ no regression ⇒ promote stands.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.50), DefaultAnchorMargin)
	exp, ctl := clearWinArms()

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE significant (anchor should hold)", got)
	}
	if !strings.Contains(got.Reason, "recent-real anchor: held") {
		t.Errorf("reason should annotate the cleared anchor, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_ExperimentalRegressesVsRecentReal_DowngradedToInconclusive(t *testing.T) {
	// Recent-real baseline 0.95 >> experimental 0.80 (by far more than the margin) ⇒
	// the shadow winner is WORSE than recent real play ⇒ PROMOTE is downgraded.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.95), DefaultAnchorMargin)
	exp, ctl := clearWinArms()

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("verdict = %q (reason %q), want INCONCLUSIVE (anchor bites)", got.Outcome, got.Reason)
	}
	if got.Significant {
		t.Errorf("a gated promote must not stay significant, got %+v", got)
	}
	if !strings.Contains(got.Reason, "promote gated by recent-real regression") {
		t.Errorf("reason should name the regression, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_WithinMargin_PromoteStands(t *testing.T) {
	// Recent-real baseline 0.81 is just ABOVE experimental 0.80, but within the
	// default margin (~0.02) ⇒ NOT a regression ⇒ promote stands. Guards against the
	// anchor biting on noise.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.81), DefaultAnchorMargin)
	exp, ctl := clearWinArms()

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote {
		t.Fatalf("verdict = %q (reason %q), want PROMOTE (within margin, not a regression)", got.Outcome, got.Reason)
	}
}

func TestVerdict_Anchor_NoRecentRealData_PromoteStandsUnchanged(t *testing.T) {
	// ok=false (the mock/shadow-only reality) ⇒ anchor SKIPPED, fail OPEN. The
	// within-experiment promote must stand so the demo/shadow loop is never blocked.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(noBaseline(), DefaultAnchorMargin)
	exp, ctl := clearWinArms()

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE significant (anchor skipped, fail open)", got)
	}
	if !strings.Contains(got.Reason, "recent-real anchor: skipped") {
		t.Errorf("reason should note the skipped anchor, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_NilAccessor_PromoteStandsUnchanged(t *testing.T) {
	// No accessor wired at all ⇒ pure within-experiment verdict, anchor absent.
	v := NewVerdict(8, 0.5, nil) // no WithRecentRealBaseline
	exp, ctl := clearWinArms()

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE significant (nil anchor)", got)
	}
	if strings.Contains(got.Reason, "recent-real anchor") {
		t.Errorf("nil anchor should not annotate the reason, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_DiscardUnaffected(t *testing.T) {
	// Even a baseline that would "regress" must NOT touch a DISCARD — the anchor only
	// gates promotion. Experimental 0.30 < control 0.70 ⇒ DISCARD; baseline 0.95 is
	// irrelevant.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.95), DefaultAnchorMargin)
	exp := armFrom(spread(10, 0.30, 0.02)...)
	ctl := armFrom(spread(10, 0.70, 0.02)...)

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomeDiscard || !got.Significant {
		t.Fatalf("verdict = %+v, want DISCARD significant (anchor must not touch discard)", got)
	}
	if strings.Contains(got.Reason, "recent-real anchor") {
		t.Errorf("discard reason should carry no anchor annotation, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_InconclusiveUnaffected(t *testing.T) {
	// A within-noise INCONCLUSIVE must be returned untouched by the anchor regardless
	// of the baseline.
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.95), DefaultAnchorMargin)
	exp := armFrom(spread(10, 0.50, 0.30)...) // huge spread ⇒ tiny effect ⇒ inconclusive
	ctl := armFrom(spread(10, 0.50, 0.30)...)

	got, ok := v.Evaluate(summaryWithObjective("endurance", exp, ctl))
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive {
		t.Fatalf("verdict = %q (reason %q), want INCONCLUSIVE", got.Outcome, got.Reason)
	}
	if strings.Contains(got.Reason, "recent-real anchor") {
		t.Errorf("inconclusive reason should carry no anchor annotation, got %q", got.Reason)
	}
}

// --- paired (CRN) path ---

func TestVerdict_Anchor_Paired_RegressionDowngrades(t *testing.T) {
	// Paired promote (mean(d)=0.30, tight spread ⇒ large d_z). The experimental ARM
	// mean (0.80) is carried on the Summary's arms; baseline 0.95 >> 0.80 ⇒ regression
	// ⇒ the paired promote is downgraded.
	v := NewVerdictWithPairs(8, 5, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.95), DefaultAnchorMargin)
	pd := pairDiffFrom(0.28, 0.32, 0.29, 0.31, 0.30, 0.30)
	s := journal.Summary{
		ExperimentID: "exp_paired",
		Objective:    "endurance",
		Arms: map[string]*journal.ArmStat{
			ArmExperimental: armFrom(spread(6, 0.80, 0.02)...),
			ArmControl:      armFrom(spread(6, 0.50, 0.02)...),
		},
		PairDiff: pd,
	}

	got, ok := v.Evaluate(s)
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != OutcomeInconclusive || got.Significant {
		t.Fatalf("verdict = %+v, want INCONCLUSIVE (paired anchor bites)", got)
	}
	if !strings.Contains(got.Reason, "promote gated by recent-real regression") {
		t.Errorf("reason should name the regression, got %q", got.Reason)
	}
}

func TestVerdict_Anchor_Paired_NoRealData_PromoteStands(t *testing.T) {
	// Same paired promote, but no real data ⇒ anchor skipped ⇒ promote stands.
	v := NewVerdictWithPairs(8, 5, 0.5, nil).WithRecentRealBaseline(noBaseline(), DefaultAnchorMargin)
	pd := pairDiffFrom(0.28, 0.32, 0.29, 0.31, 0.30, 0.30)
	s := journal.Summary{
		ExperimentID: "exp_paired",
		Objective:    "endurance",
		Arms: map[string]*journal.ArmStat{
			ArmExperimental: armFrom(spread(6, 0.80, 0.02)...),
			ArmControl:      armFrom(spread(6, 0.50, 0.02)...),
		},
		PairDiff: pd,
	}

	got, ok := v.Evaluate(s)
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if got.Outcome != CohortOutcomePromote || !got.Significant {
		t.Fatalf("verdict = %+v, want PROMOTE significant (paired anchor skipped)", got)
	}
}

func TestVerdict_WithRecentRealBaseline_NonPositiveMarginDefaults(t *testing.T) {
	v := NewVerdict(8, 0.5, nil).WithRecentRealBaseline(fixedBaseline(0.50), 0)
	if v.anchorMargin != DefaultAnchorMargin {
		t.Fatalf("non-positive margin should fall back to DefaultAnchorMargin=%v, got %v", DefaultAnchorMargin, v.anchorMargin)
	}
}
