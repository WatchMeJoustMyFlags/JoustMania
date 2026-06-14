package experiment

import (
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/joustmania/agent/experiment/journal"
)

// verdict.go implements the CohortVerdict seam (design §8.3, epic #982 decision
// 1): the per-arm aggregation + arm-comparison that turns a journal.Summary's raw
// Welford state into a promote / discard / inconclusive verdict. It is the
// extension of the M7-7 single-run FitnessMeasurer (validate.go) from "one batch →
// one number" to "a cohort → per-arm aggregates → a verdict".
//
// DEFAULT RULE (decision 1): a min-N + effect-size gate. Both arms must clear a
// configured minimum count AND the standardized effect size between the
// experimental and control arms must clear a configured threshold; otherwise the
// verdict is INCONCLUSIVE (under-powered or within noise). This is the
// conservative fallback the maintainer chose over a Welch t-test — it never
// promotes on a thin sample, so an under-powered cohort yields inconclusive, never
// a false positive (#979 acceptance).
//
// SWAPPABLE (design §8.3): the comparison itself is a CohortStat strategy. The
// default is effectSizeGate (Cohen's d); a Welch t-test or a non-parametric test
// can replace it later WITHOUT touching the CohortVerdict seam — the registry only
// ever sees journal.Verdict.
//
// CONTROL ARM is the PRIMARY decision basis (decision 5): the within-experiment
// shadow control arm is differenced against the experimental arm here. The
// recent-real baseline (the M7-7 validate.Baseline seam) is a SECONDARY sanity
// anchor; it is NOT wired through this seam because CohortVerdict.Evaluate is
// handed only a Summary (no ctx / Proposal to call Baseline.Fitness against) — see
// the wiring note at the bottom of this file. Shipping the within-experiment
// comparison (the primary basis) and deferring the baseline cross-check is the
// path the issue authorizes.

// Default gate thresholds. Tuned conservative: a verdict needs a non-trivial
// sample per arm and a medium standardized effect before it commits.
const (
	// DefaultMinNPerArm is the minimum Count each arm must reach before a
	// conclusive verdict. Below this for either arm ⇒ INCONCLUSIVE regardless of
	// the observed delta (under-powered). 8 is a deliberately small floor for
	// cheap headless shadow games; tune via AGENT_VERDICT_MIN_N.
	DefaultMinNPerArm = 8

	// DefaultEffectThreshold is the minimum |Cohen's d| (standardized mean
	// difference) for a promote/discard verdict. 0.5 ≈ Cohen's "medium" effect.
	// Below this magnitude (with N met) the arms are within-noise ⇒ INCONCLUSIVE.
	// Tune via AGENT_VERDICT_EFFECT_THRESHOLD.
	DefaultEffectThreshold = 0.5

	// DefaultMinPairs is the minimum number of COMPLETED Common-Random-Numbers pairs
	// (#1003) the PairDiff accumulator must reach before the PAIRED verdict path is
	// conclusive — the paired analog of DefaultMinNPerArm. Because CRN removes the
	// shared game variance inside each per-pair difference, far fewer pairs are needed
	// for the same power than the two-arm path needs per arm, so this floor is smaller.
	// Below it the paired path is under-powered and the verdict falls back / stays
	// inconclusive. Tune via AGENT_VERDICT_MIN_PAIRS.
	DefaultMinPairs = 5
)

const (
	minNEnv     = "AGENT_VERDICT_MIN_N"
	effectEnv   = "AGENT_VERDICT_EFFECT_THRESHOLD"
	minPairsEnv = "AGENT_VERDICT_MIN_PAIRS"
)

// CohortStat is the swappable arm-comparison strategy (design §8.3). Given the two
// arms' raw running statistics it returns a standardized effect size (experimental
// relative to control: positive = experimental better, negative = worse) and
// whether that effect could be computed at all (false ⇒ variance undefined /
// degenerate, treat as no signal). The default effectSizeGate implements Cohen's
// d; a Welch / non-parametric statistic can satisfy the same interface later.
type CohortStat interface {
	// Effect returns the standardized effect of experimental vs control and a bool
	// reporting whether it is defined. The caller (CohortVerdict) still applies the
	// min-N gate; this strategy only quantifies the difference.
	Effect(experimental, control journal.Welford) (effect float64, ok bool)
}

// effectSizeGate is the default CohortStat: Cohen's d, the difference of means in
// units of the pooled standard deviation. It uses the UNBIASED sample variance
// M2/(Count−1) per arm — this is exactly why the seam is handed the raw Summary
// (with raw M2/Count) rather than the CompactView (which exposes only the derived
// POPULATION variance M2/Count). Population variance would under-state spread at
// small N and inflate the effect size toward a premature verdict; the sample
// (n−1) form is the conservative, correct choice here.
type effectSizeGate struct{}

// Effect computes Cohen's d = (mean_exp − mean_control) / pooledSD, using the
// pooled UNBIASED sample variance. It returns ok=false when either arm has fewer
// than 2 samples (sample variance undefined) or the pooled SD is zero (no spread —
// the standardized effect is undefined / infinite, which the min-N gate handles
// separately so we never divide by zero).
func (effectSizeGate) Effect(experimental, control journal.Welford) (float64, bool) {
	if experimental.Count < 2 || control.Count < 2 {
		return 0, false
	}
	// Unbiased sample variance M2/(n−1) per arm — NOT the population M2/n.
	varExp := experimental.M2 / float64(experimental.Count-1)
	varCtl := control.M2 / float64(control.Count-1)

	// Pooled standard deviation (the standard two-sample pooled estimator).
	dfExp := float64(experimental.Count - 1)
	dfCtl := float64(control.Count - 1)
	pooledVar := (dfExp*varExp + dfCtl*varCtl) / (dfExp + dfCtl)
	pooledSD := math.Sqrt(pooledVar)
	if pooledSD == 0 {
		return 0, false
	}
	return (experimental.Mean - control.Mean) / pooledSD, true
}

// Verdict is the default CohortVerdict implementation (the min-N + effect-size
// gate). It is constructed via NewVerdict / NewVerdictFromEnv and injected into the
// registry as the CohortVerdict seam. Higher fitness is better (the convention the
// FitnessMeasurer and decision loop already use), so a positive effect means the
// experimental arm beat control.
type Verdict struct {
	// mu guards the live-tunable thresholds (minN / minPairs) so SetThresholds
	// (#1044, called from the experiment loop's tick goroutine) and Evaluate (called
	// under the registry lock from a ConcludeGame goroutine) never race. The other
	// fields (effectThreshold / stat) are construction-immutable and read without the
	// lock.
	mu sync.RWMutex
	// minN is the minimum Count both arms must reach for a conclusive TWO-ARM verdict
	// (the fallback path). Live-tunable via SetThresholds (#1044, verdict_min_n).
	minN int
	// minPairs is the minimum completed-pair count PairDiff must reach for a conclusive
	// PAIRED verdict (the primary path when CRN pairs exist). Live-tunable via
	// SetThresholds (#1044, verdict_min_pairs).
	minPairs int
	// effectThreshold is the minimum |effect| for a promote/discard verdict. It gates
	// both the two-arm Cohen's d and the paired d_z (both are standardized effects).
	effectThreshold float64
	// stat is the swappable comparison strategy (default effectSizeGate).
	stat CohortStat
}

// SetThresholds live-updates the min-N and min-pairs gates (#1044) so a hot-reload
// of verdict_min_n / verdict_min_pairs takes effect on the NEXT Evaluate with no
// restart. A non-positive value is IGNORED for that gate (keeps the current value),
// mirroring the constructor's fail-safe floor — a transient flagd hiccup that
// resolves a knob to 0 must not silently make every thin sample "conclusive". It is
// safe to call concurrently with Evaluate; both take v.mu.
func (v *Verdict) SetThresholds(minN, minPairs int) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if minN > 0 {
		v.minN = minN
	}
	if minPairs > 0 {
		v.minPairs = minPairs
	}
}

// NewVerdict builds a Verdict with explicit thresholds. A non-positive minN falls
// back to DefaultMinNPerArm; a non-positive effectThreshold falls back to
// DefaultEffectThreshold. A nil stat falls back to the default Cohen's d gate.
//
// The paired-path min-pairs gate is set to the SAME minN value — a caller that asks
// for a conclusive verdict at minN games per arm wants the paired path conclusive at
// minN completed PAIRS too (the paired analog of min-N). This keeps the two paths
// coherent for the single-knob caller; use NewVerdictWithPairs to gate pairs
// independently (e.g. the smaller DefaultMinPairs floor, since CRN needs fewer pairs
// for the same power).
func NewVerdict(minN int, effectThreshold float64, stat CohortStat) *Verdict {
	return NewVerdictWithPairs(minN, minN, effectThreshold, stat)
}

// NewVerdictWithPairs builds a Verdict with explicit two-arm min-N AND paired
// min-pairs gates. A non-positive minN falls back to DefaultMinNPerArm; a
// non-positive minPairs falls back to DefaultMinPairs; a non-positive
// effectThreshold falls back to DefaultEffectThreshold; a nil stat falls back to the
// default Cohen's d gate.
func NewVerdictWithPairs(minN, minPairs int, effectThreshold float64, stat CohortStat) *Verdict {
	if minN <= 0 {
		minN = DefaultMinNPerArm
	}
	if minPairs <= 0 {
		minPairs = DefaultMinPairs
	}
	if effectThreshold <= 0 {
		effectThreshold = DefaultEffectThreshold
	}
	if stat == nil {
		stat = effectSizeGate{}
	}
	return &Verdict{minN: minN, minPairs: minPairs, effectThreshold: effectThreshold, stat: stat}
}

// MinNFromEnv resolves the verdict min-N-per-arm gate from AGENT_VERDICT_MIN_N,
// falling back to DefaultMinNPerArm on an unset, empty, non-numeric, or
// non-positive value. It is exported so the registry can mirror the SAME min-N
// the verdict uses (for target_n reconciliation and the inconclusive-past-target
// termination guard, #1001) without duplicating the parse or widening the
// CohortVerdict seam.
func MinNFromEnv() int {
	if v := strings.TrimSpace(os.Getenv(minNEnv)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return DefaultMinNPerArm
}

// MinPairsFromEnv resolves the paired-verdict min-completed-pairs gate from
// AGENT_VERDICT_MIN_PAIRS, falling back to DefaultMinPairs on an unset, empty,
// non-numeric, or non-positive value. It mirrors MinNFromEnv for the paired path so
// the registry could reconcile a paired target the same way (#1001) without widening
// the seam.
func MinPairsFromEnv() int {
	if v := strings.TrimSpace(os.Getenv(minPairsEnv)); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return DefaultMinPairs
}

// NewVerdictFromEnv builds a Verdict from AGENT_VERDICT_MIN_N /
// AGENT_VERDICT_MIN_PAIRS / AGENT_VERDICT_EFFECT_THRESHOLD, falling back to the
// defaults on an unset, empty, or invalid value. The default Cohen's d strategy is
// used for the two-arm fallback path.
func NewVerdictFromEnv() *Verdict {
	minN := MinNFromEnv()
	minPairs := MinPairsFromEnv()
	threshold := DefaultEffectThreshold
	if v := strings.TrimSpace(os.Getenv(effectEnv)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			threshold = f
		}
	}
	return NewVerdictWithPairs(minN, minPairs, threshold, effectSizeGate{})
}

// Evaluate computes the current verdict for an experiment from its rolling
// Summary (the CohortVerdict seam contract, design §8.3).
//
// It reads the canonical experimental and control arms (ArmExperimental /
// ArmControl) and applies the min-N + effect-size gate:
//
//   - Either arm Count < minN ⇒ INCONCLUSIVE (under-powered). Recorded so the
//     registry keeps the experiment RUNNING; never a promote/discard on a thin
//     sample.
//   - Both arms ≥ minN AND effect ≥ +threshold ⇒ PROMOTE (experimental clearly
//     better). Significant.
//   - Both arms ≥ minN AND effect ≤ −threshold ⇒ DISCARD (experimental clearly
//     worse). Significant.
//   - Otherwise (N met but |effect| < threshold, or effect undefined) ⇒
//     INCONCLUSIVE (within noise). Recorded, keeps running.
//
// The bool reports whether a verdict could be computed at all. It is true whenever
// both canonical arms are present in the Summary (even when the verdict is
// under-powered INCONCLUSIVE — that IS a meaningful interim verdict the registry
// records). It is false only when the Summary lacks the canonical arms entirely,
// so the registry leaves any prior verdict untouched rather than overwriting it
// with a vacuous one.
func (v *Verdict) Evaluate(s journal.Summary) (journal.Verdict, bool) {
	// Snapshot the live-tunable gates ONCE under the lock (#1044) so a concurrent
	// SetThresholds cannot tear them mid-evaluation; the rest of the method uses the
	// locals.
	v.mu.RLock()
	minN := v.minN
	minPairs := v.minPairs
	v.mu.RUnlock()

	// PAIRED PATH (#1004): when seed-matched (experimental, control) pairs have
	// completed (#1003), difference fitness PER PAIR. Common Random Numbers remove the
	// shared game variance inside each d_i = f_exp_i − f_ctl_i, so the standardized
	// paired effect d_z = mean(d)/sd(d) is a far tighter estimator than the two-arm
	// pooled Cohen's d and reaches a conclusive verdict at far fewer games. Used
	// whenever at least one pair has completed; otherwise we fall through to the
	// two-arm path so legacy/unpaired experiments are unaffected (the fallback).
	if s.PairDiff != nil && s.PairDiff.Count > 0 {
		return v.evaluatePaired(*s.PairDiff, minPairs)
	}

	expStat, expOK := s.Arms[ArmExperimental]
	ctlStat, ctlOK := s.Arms[ArmControl]
	if !expOK || !ctlOK || expStat == nil || ctlStat == nil {
		// The canonical two-arm shape is absent — nothing to compare; do not
		// clobber any prior verdict.
		return journal.Verdict{}, false
	}

	exp := expStat.Welford
	ctl := ctlStat.Welford
	delta := exp.Mean - ctl.Mean

	// Min-N gate FIRST: an under-powered cohort is inconclusive regardless of the
	// observed delta or effect (the #979 acceptance: never a false positive).
	if exp.Count < minN || ctl.Count < minN {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       delta,
			Significant: false,
			Reason: fmt.Sprintf("under-powered: n_experimental=%d n_control=%d < min_n=%d",
				exp.Count, ctl.Count, minN),
		}, true
	}

	// Effect-size gate: compute the standardized effect via the swappable strategy.
	effect, ok := v.stat.Effect(exp, ctl)
	if !ok {
		// Variance undefined / degenerate (e.g. zero-spread arms) — N is met but we
		// cannot standardize the difference, so treat it as no signal.
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       delta,
			Significant: false,
			Reason:      "effect size undefined (degenerate variance)",
		}, true
	}

	switch {
	case effect >= v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomePromote,
			Delta:       delta,
			Significant: true,
			Reason: fmt.Sprintf("experimental better: effect=%.3f >= threshold=%.2f (n=%d/%d)",
				effect, v.effectThreshold, exp.Count, ctl.Count),
		}, true
	case effect <= -v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomeDiscard,
			Delta:       delta,
			Significant: true,
			Reason: fmt.Sprintf("experimental worse: effect=%.3f <= -threshold=%.2f (n=%d/%d)",
				effect, v.effectThreshold, exp.Count, ctl.Count),
		}, true
	default:
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       delta,
			Significant: false,
			Reason: fmt.Sprintf("within noise: |effect|=%.3f < threshold=%.2f (n=%d/%d)",
				math.Abs(effect), v.effectThreshold, exp.Count, ctl.Count),
		}, true
	}
}

// evaluatePaired computes the verdict from the per-PAIR difference accumulator
// (#1004). The statistic is the standardized paired effect d_z = mean(d) / sd(d),
// where d_i = f_experimental_i − f_control_i and sd(d) is the UNBIASED sample
// standard deviation sqrt(M2/(n−1)) over the n completed pairs — the same n−1
// convention the two-arm gate uses (small-N honest, never inflating the effect).
// It applies:
//
//   - n < minPairs ⇒ INCONCLUSIVE (under-powered paired sample). Mirrors the two-arm
//     min-N gate: never a promote/discard on too few pairs.
//   - sd(d) == 0 (every pair identical, e.g. a no-op flag with mean(d)==0, or a
//     single pair) ⇒ INCONCLUSIVE (effect undefined). A genuine no-op flag produces
//     per-pair deltas tightly centered on 0, so this correctly reads as no signal.
//   - d_z >= +threshold ⇒ PROMOTE; d_z <= −threshold ⇒ DISCARD; else INCONCLUSIVE.
//
// Delta carries mean(d) (the average per-pair fitness improvement) for the audit
// trail. The bool is always true: a non-nil non-empty PairDiff is a meaningful
// (possibly interim) verdict the registry should record.
func (v *Verdict) evaluatePaired(pd journal.Welford, minPairs int) (journal.Verdict, bool) {
	meanD := pd.Mean

	if pd.Count < minPairs {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason: fmt.Sprintf("paired under-powered: pairs=%d < min_pairs=%d",
				pd.Count, minPairs),
		}, true
	}

	// Unbiased sample SD of the per-pair differences: sqrt(M2/(n−1)). Undefined for a
	// single pair (handled by the min-pairs gate above for the default, but guard
	// explicitly so a min_pairs=1 override can never divide by zero).
	if pd.Count < 2 {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason:      "paired effect undefined: need >= 2 pairs for a paired SD",
		}, true
	}
	sdD := math.Sqrt(pd.M2 / float64(pd.Count-1))
	if sdD == 0 {
		// Zero spread across pairs — the per-pair difference is a constant. The
		// standardized effect is undefined (the magnitude is in meanD, but with no
		// spread there is no scale to standardize against), so treat it as no signal.
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason: fmt.Sprintf("paired effect undefined (zero spread across %d pairs)",
				pd.Count),
		}, true
	}

	dz := meanD / sdD
	switch {
	case dz >= v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomePromote,
			Delta:       meanD,
			Significant: true,
			Reason: fmt.Sprintf("paired: experimental better: d_z=%.3f >= threshold=%.2f (pairs=%d)",
				dz, v.effectThreshold, pd.Count),
		}, true
	case dz <= -v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomeDiscard,
			Delta:       meanD,
			Significant: true,
			Reason: fmt.Sprintf("paired: experimental worse: d_z=%.3f <= -threshold=%.2f (pairs=%d)",
				dz, v.effectThreshold, pd.Count),
		}, true
	default:
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason: fmt.Sprintf("paired within noise: |d_z|=%.3f < threshold=%.2f (pairs=%d)",
				math.Abs(dz), v.effectThreshold, pd.Count),
		}, true
	}
}

// Wiring note (recent-real baseline, design §8.3 secondary anchor — DEFERRED):
//
// The within-experiment control arm is the PRIMARY decision basis and is fully
// implemented above. The recent-real baseline (validate.Baseline.Fitness) is the
// design's SECONDARY sanity cross-check. It is NOT wired here because the
// CohortVerdict seam (registry_seams.go) deliberately passes only a
// journal.Summary — there is no ctx / Proposal in Evaluate's signature to call
// Baseline.Fitness(ctx, p) against, and the registry's ConcludeGame call site does
// not thread one through. Plumbing it would mean widening the seam (and the
// registry call sites), which is out of scope for this issue. Follow-up: either
// fold a recent-real baseline sample into a third pseudo-arm on the Summary at
// game-end, or annotate the Verdict.Reason with a baseline cross-check after the
// registry gains access to Baseline. Tracked as a follow-up on epic #982.
