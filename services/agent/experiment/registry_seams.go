package experiment

import (
	"context"

	"github.com/joustmania/agent/experiment/journal"
)

// registry_seams.go defines the INJECTED boundaries of the experiment registry
// (design §7). The registry owns ONLY the lifecycle + capacity + book-keeping
// logic; every consequential effect — spawning a shadow game, computing a
// cohort verdict, promoting a validated experiment, and writing/stripping the
// shadow-scoped targeting rule — is an interface the registry calls.
//
// This mirrors the Gate/Writer/Validator/Promoter split already established in
// this package: the registry stays unit-testable with recording fakes, and the
// real implementations are constructed only in main.go (or the relevant
// follow-up issue). The three forward-looking seams plug in as:
//
//   - ShadowSpawner  → #976 (the game-coordinator shadow-spawn RPC). Until then a
//                      no-op stub is injected and no real game is started.
//   - CohortVerdict  → #979 (the per-arm Welford aggregation + significance test).
//                      The registry only stores the verdict it returns.
//   - Promoter       → #961/#980 (the existing promote.Promoter). The registry
//                      routes a CONCLUDED+promote outcome to it; it never
//                      reimplements the promotion safety rail.
//   - TargetingWriter → #977 (the Gate/Writer). The registry writes the
//                      experiment-scoped targeting on RUNNING and strips it on
//                      teardown; it never builds the JSONLogic itself.

// ShadowSpawner starts ONE shadow game bound to (experimentID, arm) and returns
// the coordinator-assigned game_id. It is the spawn seam (design §5 / §7.1):
// when capacity allows a new shadow game for an experiment, the registry DECIDES
// the (experiment_id, arm) ground-truth-at-spawn and hands it to the spawner,
// which is responsible for setting that experiment_id + arm on the spawned
// game's OpenFeature evaluation context so the flagd targeting (#977) resolves
// the right arm.
//
// PRODUCTION (#976, not built here): the real implementation calls the
// game-coordinator shadow-spawn RPC (the M6 headless shadow-game harness),
// passing experiment_id + arm so the game resolves the experimental variant (or
// the control fall-through). A control-arm game MUST still be game_kind="shadow"
// — the hard real-protection invariant (epic #982 decision 7). The no-op stub
// (NoopShadowSpawner) returns an error so a registry wired without a real spawner
// never silently believes a game started.
type ShadowSpawner interface {
	// Spawn starts a shadow game for the experiment's arm and returns its game_id.
	// An error means no game was started; the registry records nothing and the
	// capacity slot is not consumed.
	Spawn(ctx context.Context, experimentID, arm string) (gameID string, err error)
}

// Outcome labels mirror the journal Verdict.Outcome strings (and the
// experiment.Outcome / promote verdicts) without importing them, so the registry
// can branch on a verdict it stores verbatim. The CohortVerdict seam (#979)
// supplies one of these.
const (
	// OutcomeInconclusive — under-powered (min-N not reached) or no significant
	// difference yet. The experiment keeps running (or concludes under-powered).
	OutcomeInconclusive = "inconclusive"
	// CohortOutcomePromote — the experimental arm cleared the significance + effect
	// bar; route to the Promoter on conclusion.
	CohortOutcomePromote = "promote"
	// CohortOutcomeDiscard — concluded with no promotable improvement; discard.
	CohortOutcomeDiscard = "discard"
)

// CohortVerdict computes the current verdict for an experiment from its per-arm
// running statistics. It is the aggregation seam (design §8): the registry calls
// it after each game concludes (interim) and on conclusion (final), records the
// returned journal.Verdict, and transitions status on it. The registry does NOT
// implement the statistics — #979 does.
//
// PRODUCTION (#979, not built here): the real implementation reads the per-arm
// Welford state from the journal CompactView/Summary and applies the default
// min-N + effect-size gate (epic #982 decision 1; swappable to Welch /
// non-parametric later). It returns OutcomeInconclusive until each arm clears the
// target N AND the difference clears the effect bar.
type CohortVerdict interface {
	// Evaluate returns the current verdict for the experiment given its rolling
	// Summary — the FULL per-arm Welford state (Count/Mean/raw M2), NOT the
	// CompactView (which exposes only the derived POPULATION variance and drops the
	// raw M2). The significance test (#979) needs the raw M2 + count to compute an
	// unbiased / sample (n-1) variance or a Welch estimator, so the seam consumes
	// Summary rather than CompactView (see journal.ArmView's downstream note). The
	// bool reports whether a verdict could be computed at all (false ⇒ leave the
	// prior verdict untouched).
	Evaluate(s journal.Summary) (journal.Verdict, bool)
}

// Promoter routes a CONCLUDED experiment with a promote verdict to the existing
// promotion flow (#961/#980). The registry injects it as an interface so it never
// reimplements the promotion safety rail; promote.Promoter (adapted) satisfies it.
//
// PRODUCTION (#961/#980, not built here): the real implementation assembles
// promote.Evidence from the cohort verdict (FitnessBefore = control mean,
// FitnessAfter = experimental mean, SyntheticGames = N) and calls
// promote.Promoter.Promote under the live mode/target flags.
type Promoter interface {
	// Promote acts on a concluded, promote-verdict experiment. The error is
	// recorded but never blocks teardown — a failed promotion still releases
	// capacity (the experiment is concluded either way).
	Promote(ctx context.Context, view journal.CompactView) error
}

// TargetingWriter scopes / un-scopes the experiment's shadow targeting branch on
// the game flagset (#977). The registry calls Apply when an experiment starts
// RUNNING and Strip on teardown (any terminal status), so the flagd rule tracks
// the live experiment set. It is an interface so the registry stays free of the
// game.json write surface; a thin adapter over the Gate/Writer satisfies it.
//
// PRODUCTION (#977): Apply runs a Proposal{FlagKey, ExperimentalValue,
// ExperimentID} through the Gate (which Writes it). Strip removes that
// experiment's chained-if branch (Writer.stripShadowBranch) — invariant-safe
// because removing a shadow branch only ever affects shadow games.
type TargetingWriter interface {
	// Apply writes the experiment-scoped shadow targeting for the proposal.
	Apply(ctx context.Context, p Proposal) error
	// Strip removes the experiment's targeting branch for (flagKey, experimentID).
	// A missing branch is a no-op (idempotent teardown).
	Strip(ctx context.Context, flagKey, experimentID string) error
}
