package promote

// experiment_seam.go is the #980 adapter that lets the experiment REGISTRY
// (#978) drive the EXISTING #961 Promoter unchanged. It implements the registry's
// promotion seam (experiment.Promoter: Promote(ctx, journal.CompactView) error)
// as a thin adapter over *Promoter — it REUSES the env-gated mode/target dispatch
// and the whole safety rail (NewGitHubClientFromEnv / kill-switch / safe issue+
// local defaults). It does NOT reimplement promotion, the env gates, or the
// kill-switch (design §10).
//
// IMPORT-CYCLE NOTE (a divergence from the issue's suggested file path): the
// adapter lives in the `promote` package, NOT in `experiment` as the issue sketched
// (experiment/promote_seam.go). `promote` already imports `experiment` (promote.go
// builds promote.Promote from experiment.Proposal/Decision), so an adapter in
// `experiment` that called into `promote` would form an import cycle. Placing it
// here keeps the dependency one-directional (promote → experiment) and lets the
// adapter satisfy experiment.Promoter without any change to the registry seam.
//
// WHY THE SEAM SIGNATURE NEED NOT WIDEN: the registry passes a journal.CompactView,
// which already carries everything the #961 Promoter needs to learn WHICH flag/value
// to promote and the fitness Evidence:
//
//   - the flag identity + candidate value come from CompactView.Intent
//     (FlagKey, ExperimentalValue, Hypothesis, ExperimentID) — the immutable
//     experiment intent the journal stores at creation;
//   - the fitness Evidence comes from the per-arm rolling aggregates
//     (CompactView.Arms[control].Mean → FitnessBefore, Arms[experimental].Mean →
//     FitnessAfter, total N across arms → SyntheticGames).
//
// So the adapter resolves the experiment's intent straight off the CompactView it
// is handed; the registry seam stays CompactView-only (no widening, no extra intent
// reference needed).

import (
	"context"
	"fmt"
	"strings"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
)

// ConfigResolver resolves the LIVE promotion Config (mode/target + the agent
// kill-switch) for one promotion. It is injected so the adapter stays free of an
// OpenFeature dependency — the SAME "package owns logic, caller owns flagd" split
// the Promoter, Gate, Writer, and Validator already use. main.go supplies a
// resolver that reads flags.Promotion(ctx) + the agent `enabled` snapshot; tests
// supply a fixed Config. A nil resolver yields the SAFE default (issue/local,
// enabled=false) so the adapter can NEVER reach a real action without an explicit
// resolver — the safety-rail floor is preserved.
type ConfigResolver func(ctx context.Context) Config

// ExperimentPromoter adapts the #961 Promoter to the experiment registry's
// Promoter seam (#978/#980). The registry calls Promote on a CONCLUDED+promote
// verdict (it never routes a discard/inconclusive here), and this adapter turns the
// journal.CompactView into the (Proposal, Decision, Config, Evidence) the existing
// Promoter consumes — then calls it UNCHANGED.
type ExperimentPromoter struct {
	// promoter is the existing #961 Promoter. Its env-gated seams (github/git/
	// realDef) and safety rail are reused verbatim; this adapter only assembles its
	// inputs.
	promoter *Promoter
	// resolveConfig resolves the live mode/target/kill-switch each promotion (never
	// cached), so flipping code_improvement.mode takes effect on the NEXT promotion.
	resolveConfig ConfigResolver
}

// NewExperimentPromoter builds the registry-seam adapter over an existing #961
// Promoter. p MUST be non-nil (the whole point is to reuse it). resolveConfig may
// be nil — then every promotion runs under the SAFE default Config (issue/local,
// disabled), i.e. the adapter degrades to the fail-closed floor rather than
// fabricating a privileged mode. The returned *ExperimentPromoter satisfies
// experiment.Promoter and is the concrete value injected into
// experiment.Registry (cfg.Promoter); the agent main loop wiring is separate.
func NewExperimentPromoter(p *Promoter, resolveConfig ConfigResolver) *ExperimentPromoter {
	if resolveConfig == nil {
		// Fail-closed floor: no resolver ⇒ never autonomous, never github.
		resolveConfig = func(context.Context) Config {
			return Config{Mode: ModeIssue, Target: TargetLocal, Enabled: false}
		}
	}
	return &ExperimentPromoter{promoter: p, resolveConfig: resolveConfig}
}

// Promote implements experiment.Promoter. The registry hands it the concluded
// experiment's bounded journal view; it builds the #961 Promoter's inputs from the
// immutable intent + the per-arm rolling aggregates and calls Promote UNCHANGED.
//
// Evidence mapping (design §10):
//
//	FitnessBefore  = control-arm mean      (CompactView.Arms[ArmControl].Mean)
//	FitnessAfter   = experimental-arm mean (CompactView.Arms[ArmExperimental].Mean)
//	FitnessDelta   = after − before
//	SyntheticGames = total N across all arms (sum of per-arm Count)
//
// The flag/value to promote come from the intent: Proposal.FlagKey =
// Intent.FlagKey, RealDefaultValue (the autonomous candidate) =
// Intent.ExperimentalValue. The Decision is synthesized as OutcomePromote —
// the registry routes here ONLY on a promote verdict, so the underlying Promoter's
// "only a PROMOTE verdict acts" guard passes; a non-promote view (defensive) yields
// OutcomeDiscard and the Promoter no-ops. A failed promotion is returned as an error
// (recorded by the registry) but never blocks teardown.
func (a *ExperimentPromoter) Promote(ctx context.Context, view journal.CompactView) error {
	if a.promoter == nil {
		return fmt.Errorf("promote: experiment seam has no underlying promoter")
	}

	intent := view.Intent

	before := armMean(view, experiment.ArmControl)
	after := armMean(view, experiment.ArmExperimental)
	delta := after - before
	totalN := totalGames(view)

	prop := experiment.Proposal{
		FlagKey:           intent.FlagKey,
		ExperimentID:      intent.ExperimentID,
		ExperimentalValue: intent.ExperimentalValue,
		Rationale:         intent.Hypothesis,
	}

	ev := Evidence{
		FitnessBefore:    before,
		FitnessAfter:     after,
		FitnessDelta:     delta,
		SyntheticGames:   totalN,
		Reasoning:        intent.Hypothesis,
		FlagState:        flagState(intent),
		RealDefaultValue: intent.ExperimentalValue,
	}

	// The registry only calls Promote on a CONCLUDED+promote verdict; synthesize the
	// PROMOTE Decision the underlying Promoter's guard expects. A defensive
	// non-promote view (no verdict, or a verdict that is not "promote") maps to
	// OutcomeDiscard so the Promoter records a no-op rather than acting — the adapter
	// only ever ACTS on promote.
	dec := experiment.Decision{
		Outcome:       decisionOutcome(view),
		FitnessBefore: before,
		FitnessAfter:  after,
		Games:         totalN,
	}

	cfg := a.resolveConfig(ctx)

	// Reuse the #961 Promoter UNCHANGED: its env-gated dispatch, safe issue/local
	// defaults, and kill-switch (cfg.Enabled gates autonomous) apply as-is.
	res := a.promoter.Promote(ctx, prop, dec, cfg, ev)

	// A discarded outcome with a reason is a RECORDED no-op (e.g. github target not
	// enabled, or agent disabled) — the safety rail working as designed, NOT a
	// failure. Surface it as an error so the registry logs it, but it never blocks
	// teardown (the registry treats a Promote error as non-fatal). A clean applied/
	// reverted outcome returns nil.
	if res.Outcome == OutcomeDiscarded && res.Reason != "" {
		return fmt.Errorf("promote: %s (mode=%s target=%s)", res.Reason, res.Mode, res.Target)
	}
	return nil
}

// armMean returns the rolling mean fitness for an arm, or 0 if the arm has no
// games yet (a never-played arm reports Count 0 / Mean 0 in the CompactView).
func armMean(view journal.CompactView, arm string) float64 {
	if v, ok := view.Arms[arm]; ok {
		return v.Mean
	}
	return 0
}

// totalGames sums the per-arm game counts — the sample size N behind the verdict
// (both arms together, per design §10 "SyntheticGames = N").
func totalGames(view journal.CompactView) int {
	total := 0
	for _, v := range view.Arms {
		total += v.Count
	}
	return total
}

// decisionOutcome maps the journal verdict carried on the view to the Promoter's
// gate. The registry routes here only on a promote verdict, so this is normally
// OutcomePromote; anything else is treated as discard (defensive — the Promoter
// then no-ops rather than acting on a non-promote view).
func decisionOutcome(view journal.CompactView) experiment.Outcome {
	if view.Verdict != nil && view.Verdict.Outcome == experiment.CohortOutcomePromote {
		return experiment.OutcomePromote
	}
	return experiment.OutcomeDiscard
}

// flagState renders a short human-readable snapshot of the experiment's flag
// context for the issue/PR body (a reviewer sees what was promoted). Best-effort;
// the Evidence body renderer tolerates an empty value.
func flagState(intent journal.Intent) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s: experimental=%v", intent.FlagKey, intent.ExperimentalValue)
	if intent.Objective != "" {
		fmt.Fprintf(&b, "; objective=%s", intent.Objective)
	}
	if intent.ExperimentID != "" {
		fmt.Fprintf(&b, "; experiment_id=%s", intent.ExperimentID)
	}
	return b.String()
}
