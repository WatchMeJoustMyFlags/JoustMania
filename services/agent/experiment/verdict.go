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
// recent-real baseline is a SECONDARY substrate-validity anchor (#992, relates
// #1017): when recent REAL play exists for the experiment's objective, a PROMOTE
// must additionally hold up against it (the experimental arm must not REGRESS below
// the recent-real baseline beyond a margin) or it is downgraded to inconclusive.
// The anchor only ever GATES a promote — it never forces one, and discard /
// inconclusive verdicts are unaffected. It FAILS OPEN: with no real-game data (the
// mock/shadow-only reality) the anchor is skipped and the within-experiment verdict
// stands. The baseline is read via an injected, nil-safe RecentRealBaseline accessor
// (wired with WithRecentRealBaseline); the objective is carried on the Summary
// (journal.Summary.Objective), so the CohortVerdict.Evaluate seam signature is
// UNCHANGED — see applyRecentRealAnchor and the wiring note at the bottom.

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

	// DefaultMinRawEffect is the PRACTICAL-significance floor (#1042): the minimum RAW
	// effect magnitude — |mean_d| (paired) or |mean_exp − mean_ctl| (two-arm) — a
	// promote/discard verdict requires, regardless of how large the STANDARDIZED effect
	// (d_z / Cohen's d) is. Below it the verdict is INCONCLUSIVE.
	//
	// WHY: on near-deterministic shadow games the per-pair difference is tiny but
	// perfectly consistent, so sd(d)→0 and d_z = mean_d/sd(d) explodes (the dry-run saw
	// d_z=-34 on a ~0.0004 fitness delta). Statistical significance ≠ practical
	// significance: a tiny-but-consistent delta is not worth promoting/discarding on.
	//
	// VALUE 0.02 = 2% of the ~0..1 fitness range. Rationale: a fitness change smaller
	// than 2% of the achievable range is below what we'd act on for a party game — it is
	// in the don't-care band — while every genuinely meaningful effect in practice (and
	// in the test suite, deltas ≥0.05) clears it comfortably. Tune via
	// AGENT_VERDICT_MIN_RAW_EFFECT.
	DefaultMinRawEffect = 0.02

	// DefaultSDFloor is the minimum-standard-deviation floor (#1042, defense in depth):
	// the denominator of d_z / Cohen's d is floored at this value so the standardized
	// statistic stays finite and meaningful when the observed spread collapses toward 0
	// (near-deterministic games). It does NOT replace the raw-effect floor above — it
	// keeps d_z from reporting an absurd magnitude in the audit trail / logs.
	//
	// VALUE 1e-4: two orders of magnitude below any real-play spread (real per-game
	// fitness noise is ~0.01+), so it never distorts a real-noise verdict — it only bites
	// in the degenerate sd→0 regime, where the raw-effect floor is the real gate anyway.
	// Tune via AGENT_VERDICT_SD_FLOOR.
	DefaultSDFloor = 1e-4
)

const (
	minNEnv      = "AGENT_VERDICT_MIN_N"
	effectEnv    = "AGENT_VERDICT_EFFECT_THRESHOLD"
	minPairsEnv  = "AGENT_VERDICT_MIN_PAIRS"
	minRawEffEnv = "AGENT_VERDICT_MIN_RAW_EFFECT"
	sdFloorEnv   = "AGENT_VERDICT_SD_FLOOR"
	// anchorMarginEnv tunes the recent-real regression margin (#992). Falls back to
	// DefaultAnchorMargin (which mirrors the #1042 raw-effect floor).
	anchorMarginEnv = "AGENT_VERDICT_ANCHOR_MARGIN"
)

// DefaultAnchorMargin is the recent-real regression margin (#992): a PROMOTE
// verdict is downgraded only when the experimental arm mean falls BELOW the
// recent-real baseline by MORE than this margin. It mirrors DefaultMinRawEffect
// (the #1042 practical-significance floor) on purpose — the same "don't act on a
// fitness change smaller than ~2% of the [0,1] range" don't-care band applies to
// the secondary cross-check, so a shadow-winner that is within noise of recent
// real play is NOT treated as a regression. Tune via AGENT_VERDICT_ANCHOR_MARGIN.
const DefaultAnchorMargin = DefaultMinRawEffect

// RecentRealBaseline is the injected secondary-anchor accessor (#992): given the
// experiment's fitness objective it returns the mean finalized fitness of the last
// N REAL (game_kind="real") games for that objective and whether such a baseline
// is AVAILABLE (ok=false ⇒ no/insufficient real samples — the mock/shadow-only
// reality). It is the read seam over the #965 FitnessStore's RecentReal, adapted
// to the (objective)→(mean, ok) shape the verdict needs WITHOUT importing the
// store. A nil accessor (the default) means the anchor is entirely absent; ok=false
// means the anchor is SKIPPED for this evaluation. Both fail OPEN — absence of real
// data must never block the within-experiment verdict (the demo/shadow loop runs
// with no real games today).
type RecentRealBaseline func(objective string) (mean float64, ok bool)

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
type effectSizeGate struct {
	// sdFloor floors the pooled SD denominator (#1042) so Cohen's d stays finite and
	// bounded when the pooled spread collapses toward 0 (near-deterministic arms). A
	// non-positive value means "no floor" (legacy behaviour — divide-by-zero is then
	// guarded by the pooledSD==0 ⇒ ok=false branch).
	sdFloor float64
}

// Effect computes Cohen's d = (mean_exp − mean_control) / pooledSD, using the
// pooled UNBIASED sample variance. The pooled SD denominator is floored at sdFloor
// (#1042) so a near-deterministic pair can't produce an absurd Cohen's d. It returns
// ok=false only when either arm has fewer than 2 samples (sample variance undefined)
// or the floored denominator is still zero (sdFloor not set AND pooled SD is exactly
// 0 — no spread, the standardized effect is undefined, which the min-N gate and the
// raw-effect floor handle separately so we never divide by zero).
func (g effectSizeGate) Effect(experimental, control journal.Welford) (float64, bool) {
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
	// Floor the denominator (#1042) so Cohen's d can't explode when pooledSD→0.
	if g.sdFloor > 0 && pooledSD < g.sdFloor {
		pooledSD = g.sdFloor
	}
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
	// minRawEffect is the PRACTICAL-significance floor (#1042): the minimum RAW effect
	// magnitude (|mean_d| paired / |mean_exp − mean_ctl| two-arm) a promote/discard
	// requires, on top of the standardized effectThreshold. Below it ⇒ INCONCLUSIVE no
	// matter how large the standardized effect is. Construction-immutable.
	minRawEffect float64
	// sdFloor is the minimum-SD floor (#1042) applied to the d_z denominator in the
	// paired path (the two-arm path's floor lives in effectSizeGate.sdFloor).
	// Construction-immutable.
	sdFloor float64
	// stat is the swappable comparison strategy (default effectSizeGate).
	stat CohortStat
	// recentRealBaseline is the SECONDARY-ANCHOR accessor (#992): the recent-real
	// fitness baseline for an objective. nil ⇒ the anchor is absent (the verdict is
	// the pure within-experiment comparison). When non-nil it GATES (never forces) a
	// PROMOTE: a shadow-winner that would regress vs recent real play is downgraded to
	// INCONCLUSIVE. Construction-immutable; wired via WithRecentRealBaseline.
	recentRealBaseline RecentRealBaseline
	// anchorMargin is the recent-real regression margin (#992). Construction-immutable.
	anchorMargin float64
}

// WithRecentRealBaseline wires the SECONDARY recent-real anchor (#992) onto the
// verdict and returns the same *Verdict for chaining. The accessor is nil-safe at
// every use site (a nil accessor leaves the verdict the pure within-experiment
// comparison), so callers that have no FitnessStore simply never call this. A
// non-positive margin falls back to DefaultAnchorMargin.
//
// It is a post-construction wiring step (not a constructor parameter) deliberately:
// every existing NewVerdict* call site stays unchanged, and the registry can build
// the verdict, then attach the baseline once the FitnessStore exists — keeping the
// seam one-directional (the verdict reads the baseline; it never reaches into the
// store). Call once at construction, before the verdict is handed to the registry.
func (v *Verdict) WithRecentRealBaseline(accessor RecentRealBaseline, margin float64) *Verdict {
	if margin <= 0 {
		margin = DefaultAnchorMargin
	}
	v.recentRealBaseline = accessor
	v.anchorMargin = margin
	return v
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
	return NewVerdictWithFloors(minN, minPairs, effectThreshold, DefaultMinRawEffect, DefaultSDFloor, stat)
}

// NewVerdictWithFloors is the full constructor (#1042): it adds the practical-
// significance floors (minRawEffect, sdFloor) to the min-N / min-pairs / effect-
// threshold gates. A non-positive minN/minPairs/effectThreshold falls back to its
// default. A non-positive minRawEffect/sdFloor falls back to its default
// (DefaultMinRawEffect / DefaultSDFloor) — pass the default explicitly to disable a
// floor only by setting the env knob, never via a silent 0.
//
// When stat is nil the default Cohen's d gate is built WITH the resolved sdFloor so
// the two-arm path is floored too; a caller-supplied stat is used verbatim (it owns
// its own denominator handling).
func NewVerdictWithFloors(minN, minPairs int, effectThreshold, minRawEffect, sdFloor float64, stat CohortStat) *Verdict {
	if minN <= 0 {
		minN = DefaultMinNPerArm
	}
	if minPairs <= 0 {
		minPairs = DefaultMinPairs
	}
	if effectThreshold <= 0 {
		effectThreshold = DefaultEffectThreshold
	}
	if minRawEffect <= 0 {
		minRawEffect = DefaultMinRawEffect
	}
	if sdFloor <= 0 {
		sdFloor = DefaultSDFloor
	}
	if stat == nil {
		stat = effectSizeGate{sdFloor: sdFloor}
	}
	return &Verdict{
		minN:            minN,
		minPairs:        minPairs,
		effectThreshold: effectThreshold,
		minRawEffect:    minRawEffect,
		sdFloor:         sdFloor,
		stat:            stat,
	}
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
	threshold := floatFromEnv(effectEnv, DefaultEffectThreshold)
	minRawEffect := floatFromEnv(minRawEffEnv, DefaultMinRawEffect)
	sdFloor := floatFromEnv(sdFloorEnv, DefaultSDFloor)
	// stat=nil so NewVerdictWithFloors builds the default gate WITH the resolved sdFloor.
	return NewVerdictWithFloors(minN, minPairs, threshold, minRawEffect, sdFloor, nil)
}

// floatFromEnv resolves a positive float knob from env, falling back to def on an
// unset, empty, non-numeric, or non-positive value (the same fail-safe convention
// MinNFromEnv / the effectThreshold parse use).
func floatFromEnv(key string, def float64) float64 {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		if f, err := strconv.ParseFloat(v, 64); err == nil && f > 0 {
			return f
		}
	}
	return def
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
		// The experimental arm mean (the value a promote would make the real default)
		// for the #992 recent-real anchor. The two-arm Welford accumulators are folded
		// alongside the paired diffs, so the experimental arm's running mean is present
		// even on the paired path; default to the per-pair mean when the arm is absent
		// (legacy/degenerate) so the anchor still has a sensible experimental value.
		expMean := s.PairDiff.Mean
		if a, ok := s.Arms[ArmExperimental]; ok && a != nil {
			expMean = a.Welford.Mean
		}
		return v.evaluatePaired(*s.PairDiff, minPairs, s.Objective, expMean)
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

	// PRACTICAL-significance floor (#1042): even a large standardized effect must be
	// backed by a raw mean difference worth acting on. A tiny-but-consistent delta
	// (near-deterministic arms ⇒ huge Cohen's d on a meaningless gap) is INCONCLUSIVE.
	if math.Abs(effect) >= v.effectThreshold && math.Abs(delta) < v.minRawEffect {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       delta,
			Significant: false,
			Reason: fmt.Sprintf("not practically significant: |delta|=%.4f < min_raw_effect=%.4f (effect=%.3f, n=%d/%d)",
				math.Abs(delta), v.minRawEffect, effect, exp.Count, ctl.Count),
		}, true
	}

	switch {
	case effect >= v.effectThreshold:
		promote := journal.Verdict{
			Outcome:     CohortOutcomePromote,
			Delta:       delta,
			Significant: true,
			Reason: fmt.Sprintf("experimental better: effect=%.3f >= threshold=%.2f, |delta|=%.4f >= min_raw=%.4f (n=%d/%d)",
				effect, v.effectThreshold, math.Abs(delta), v.minRawEffect, exp.Count, ctl.Count),
		}
		// SECONDARY anchor (#992): gate the promote against recent real play.
		return v.applyRecentRealAnchor(promote, s.Objective, exp.Mean), true
	case effect <= -v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomeDiscard,
			Delta:       delta,
			Significant: true,
			Reason: fmt.Sprintf("experimental worse: effect=%.3f <= -threshold=%.2f, |delta|=%.4f >= min_raw=%.4f (n=%d/%d)",
				effect, v.effectThreshold, math.Abs(delta), v.minRawEffect, exp.Count, ctl.Count),
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

// applyRecentRealAnchor is the SECONDARY substrate-validity guard (#992, relates
// #1017): it cross-checks a PROMOTE verdict against recent REAL play. The within-
// experiment (shadow) arm comparison is the PRIMARY decision (above); this anchor
// only ever GATES a promote — it never forces one, and it leaves discard /
// inconclusive verdicts untouched.
//
// Behaviour (a secondary anchor that fails OPEN on missing real data):
//
//   - The verdict is not a PROMOTE ⇒ returned UNCHANGED (the anchor only gates
//     promotion). Discard / inconclusive are never affected.
//   - No accessor wired (recentRealBaseline == nil) OR the accessor reports no
//     recent-real data (ok == false — the mock/shadow-only reality with no real
//     games) ⇒ the anchor is SKIPPED and the PROMOTE stands UNCHANGED. This is the
//     explicit fail-OPEN: absence of real data must not block the demo/shadow loop.
//   - Recent-real data IS available AND the experimental value would REGRESS below
//     the baseline by more than anchorMargin ⇒ the PROMOTE is DOWNGRADED to
//     INCONCLUSIVE (the within-experiment shadow winner is worse than recent real
//     play, so it is not a trustworthy real-default change), with a reason that names
//     the regression. Significant is cleared.
//   - Recent-real data available AND the experimental value holds up (≥ baseline −
//     margin) ⇒ the PROMOTE stands; the reason is annotated with the cleared anchor.
//
// experimentalValue is the experimental arm's mean fitness (the value a promotion
// would make the real default) — exp.Mean in the two-arm path, the experimental
// arm mean in the paired path. objective selects the per-objective baseline.
func (v *Verdict) applyRecentRealAnchor(verdict journal.Verdict, objective string, experimentalValue float64) journal.Verdict {
	if verdict.Outcome != CohortOutcomePromote {
		return verdict // anchor gates promotion only; discard/inconclusive unaffected.
	}
	if v.recentRealBaseline == nil {
		return verdict // anchor absent — pure within-experiment verdict stands.
	}
	baseline, ok := v.recentRealBaseline(objective)
	if !ok {
		// No / insufficient recent-real data (the mock/shadow-only reality). FAIL OPEN:
		// the within-experiment verdict stands so the demo/shadow loop is never blocked.
		verdict.Reason += " | recent-real anchor: skipped (no real-game baseline)"
		return verdict
	}
	// Regression test: the experimental value must not fall below recent real play by
	// more than the margin. margin mirrors the #1042 practical-significance floor, so a
	// shadow winner within noise of recent real play is NOT treated as a regression.
	if experimentalValue < baseline-v.anchorMargin {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       verdict.Delta,
			Significant: false,
			Reason: fmt.Sprintf("promote gated by recent-real regression: experimental=%.4f < recent_real=%.4f - margin=%.4f (objective=%q); %s",
				experimentalValue, baseline, v.anchorMargin, objective, verdict.Reason),
		}
	}
	// Anchor cleared — annotate the promote reason so the cross-check is observable.
	verdict.Reason += fmt.Sprintf(" | recent-real anchor: held (experimental=%.4f >= recent_real=%.4f - margin=%.4f)",
		experimentalValue, baseline, v.anchorMargin)
	return verdict
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
func (v *Verdict) evaluatePaired(pd journal.Welford, minPairs int, objective string, experimentalMean float64) (journal.Verdict, bool) {
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
	// Minimum-SD floor (#1042, defense in depth): floor the d_z denominator so a
	// near-deterministic pair set (sd→0) can't produce an absurd standardized effect.
	// At sd=0 exactly this also replaces the old "zero spread ⇒ undefined" bail-out:
	// the raw-effect floor below is now the gate that keeps a zero-spread no-op
	// inconclusive, so a zero-spread set with a meaningful meanD can legitimately
	// conclude (floored d_z is large AND |meanD| clears the practical floor).
	if v.sdFloor > 0 && sdD < v.sdFloor {
		sdD = v.sdFloor
	}
	if sdD == 0 {
		// Only reachable if the SD floor was disabled (sdFloor<=0) AND the spread is
		// exactly 0. The standardized effect is undefined; treat as no signal.
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason: fmt.Sprintf("paired effect undefined (zero spread across %d pairs)",
				pd.Count),
		}, true
	}

	dz := meanD / sdD

	// PRACTICAL-significance floor (#1042): a tiny-but-consistent per-pair delta yields
	// a huge d_z on near-deterministic shadow games (the dry-run's d_z=-34 on a ~0.0004
	// fitness gap). Require |meanD| to clear the minimum meaningful delta before
	// promote/discard, regardless of d_z magnitude — otherwise INCONCLUSIVE.
	if math.Abs(dz) >= v.effectThreshold && math.Abs(meanD) < v.minRawEffect {
		return journal.Verdict{
			Outcome:     OutcomeInconclusive,
			Delta:       meanD,
			Significant: false,
			Reason: fmt.Sprintf("paired not practically significant: |mean_d|=%.4f < min_raw_effect=%.4f (d_z=%.3f, pairs=%d)",
				math.Abs(meanD), v.minRawEffect, dz, pd.Count),
		}, true
	}

	switch {
	case dz >= v.effectThreshold:
		promote := journal.Verdict{
			Outcome:     CohortOutcomePromote,
			Delta:       meanD,
			Significant: true,
			Reason: fmt.Sprintf("paired: experimental better: d_z=%.3f >= threshold=%.2f, |mean_d|=%.4f >= min_raw=%.4f (pairs=%d)",
				dz, v.effectThreshold, math.Abs(meanD), v.minRawEffect, pd.Count),
		}
		// SECONDARY anchor (#992): gate the paired promote against recent real play.
		return v.applyRecentRealAnchor(promote, objective, experimentalMean), true
	case dz <= -v.effectThreshold:
		return journal.Verdict{
			Outcome:     CohortOutcomeDiscard,
			Delta:       meanD,
			Significant: true,
			Reason: fmt.Sprintf("paired: experimental worse: d_z=%.3f <= -threshold=%.2f, |mean_d|=%.4f >= min_raw=%.4f (pairs=%d)",
				dz, v.effectThreshold, math.Abs(meanD), v.minRawEffect, pd.Count),
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

// Wiring note (recent-real baseline, design §8.3 secondary anchor — IMPLEMENTED #992):
//
// The within-experiment control arm is the PRIMARY decision basis (the paired d_z /
// two-arm Cohen's d above). The recent-real baseline is the design's SECONDARY
// substrate-validity cross-check, now wired WITHOUT widening the CohortVerdict seam:
//
//   - The objective the baseline is keyed by is carried on journal.Summary.Objective
//     (a derived view field the journal copies from the immutable Intent on every
//     Summary() call), so Evaluate's signature stays Evaluate(s journal.Summary).
//   - The baseline itself is read via the injected RecentRealBaseline accessor
//     (WithRecentRealBaseline), a nil-safe func(objective)→(mean, ok) over the #965
//     FitnessStore.RecentReal. The verdict reads the baseline; it never imports or
//     reaches into the store (the seam stays one-directional).
//   - applyRecentRealAnchor gates ONLY a PROMOTE and fails OPEN (nil accessor or
//     ok=false ⇒ promote stands), so the mock/shadow-only loop is never blocked.
//
// The registry builds the verdict and attaches the baseline in experiment_loop.go
// once the FitnessStore exists. Tracked on epic #982; relates #1017 (the offline
// validity study that makes this anchor's threshold meaningful).
