package experiment

import (
	"context"
	"fmt"
	"log/slog"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
)

// validate.go implements M7-7 (#935): the SYNTHETIC VALIDATION GATE for the
// agent's flag experiments.
//
// Where this sits in the #931/#932 self-improvement track:
//
//	M7-4/M7-5 (gate.go/writer.go): the agent forms a structured {flag, value}
//	Proposal and the Writer/Gate apply it to game.json SHADOW-scoped — real games
//	are untouched by construction (see THE INVARIANT in proposal.go).
//	    ↓
//	M7-7 (THIS file): before that experiment is ever promoted to real players,
//	prove it actually helps. Establish a fitness BASELINE from recent real games
//	(#731 fitness eval), run the experiment through the M6 synthetic/shadow suite
//	(#774–#893), measure fitness with the experiment active, and DECIDE on the
//	delta: PROMOTE if it clears the threshold, DISCARD otherwise, REVERT if it
//	degraded and revert_on_degradation is set.
//	    ↓
//	M7-8 (#936): the SEPARATE gated action that actually moves the value to real
//	players (PR/human, or autonomous behind the kill-switch). M7-7 returns a
//	PROMOTE *decision*; it does NOT change the real defaultVariant itself.
//
// RESCOPE NOTE (#931/#932 "Refined design 2026-06-12"): the original #935 body
// says "apply the proposed diff to a staging copy of the service" and "patch the
// code". That is superseded — there is NO source-code diff. "Apply the
// experiment" = the shadow-scoped game-flag write the Writer already performs;
// "the patched build" = the same services running with the experiment's flag
// value active for shadow (game_kind != "real") games. So validation = run shadow
// games with the experiment active, compare fitness to the real-game baseline.
//
// INJECTABLE SEAMS (the whole point of building this in isolation, mirrors the
// Gate/Writer split): the Validator owns only the DECISION LOGIC. The two effects
// it cannot perform deterministically in a unit test — running shadow games and
// reading fitness from telemetry — are interfaces (SyntheticRunner,
// FitnessMeasurer, Baseline). Tests supply fakes; the real wiring (trigger shadow
// games via the coordinator, read fitness from the #731 evaluator / OTel) is a
// documented FOLLOW-UP, not built here. See the interface docs below for the
// exact production implementation each seam expects.

// Outcome is the Validator's verdict on an experiment after synthetic validation.
// The set is closed so the audit trail / dashboards can enumerate every result.
type Outcome string

const (
	// OutcomePromote — fitness_after exceeded fitness_before by MORE than the
	// improvement threshold. The experiment earned promotion. M7-7 stops here and
	// returns this verdict; the real-default change is M7-8's (#936) gated job.
	OutcomePromote Outcome = "promote"
	// OutcomeDiscard — the improvement did not clear the threshold (it may even be
	// slightly positive, just not enough). The experiment is dropped; nothing is
	// promoted. The shadow experiment write may stay in place for the next
	// proposal to overwrite (idempotent ExperimentVariant) — discarding is a
	// DECISION, not a rollback.
	OutcomeDiscard Outcome = "discard"
	// OutcomeRevert — fitness_after was strictly WORSE than baseline (degradation)
	// AND revert_on_degradation was true, so the active experiment was rolled back
	// via Reverter. Distinct from Discard so a degradation-driven rollback is
	// attributable separately from a merely-insufficient improvement.
	OutcomeRevert Outcome = "revert"
)

// Decision is the full result of one Validate cycle: the verdict plus the
// fitness numbers it rested on. Returned to the caller (the decision loop / M7-8)
// so the promote path can act on Outcome without re-reading the span.
type Decision struct {
	// Outcome is the verdict (promote/discard/revert).
	Outcome Outcome
	// FitnessBefore is the baseline fitness from recent real games.
	FitnessBefore float64
	// FitnessAfter is the fitness measured over the synthetic suite with the
	// experiment active. Zero/meaningless when Err is non-nil (failed closed).
	FitnessAfter float64
	// Games is the number of synthetic games actually run (the resolved
	// validation_games flag value).
	Games int
	// Reverted is true only on OutcomeRevert (degradation + revert flag).
	Reverted bool
	// Err, when non-nil, means the cycle could NOT complete (baseline unavailable,
	// synthetic run failed, measurement failed). The Validator FAILS CLOSED: on an
	// error Outcome is OutcomeDiscard and nothing is ever promoted on a broken
	// fitness signal. The caller can inspect Err for telemetry / retry; it has
	// already been recorded on the span.
	Err error
}

// Config is the live, per-cycle flag configuration the Validator reads (#935
// flags, resolved fresh each Validate so a stage retune takes effect on the next
// cycle — same never-cached contract as the #847 llm.* gate flags). The caller
// resolves these via flags.Validation(ctx) (see flags/flags.go) and passes the
// value in, so the experiment package stays free of an OpenFeature dependency
// (the Gate/Writer already follow this "package owns logic, caller owns flagd"
// split).
type Config struct {
	// ValidationGames is how many synthetic games to run for the experiment
	// (code_improvement.validation_games, default 5). Clamped to >= 1 by Validate:
	// a zero/negative game count cannot measure anything, so a misconfigured flag
	// runs at least one game rather than promoting on no evidence.
	ValidationGames int
	// ImprovementThreshold is the minimum fitness improvement (after - before) the
	// experiment must EXCEED to be promoted
	// (code_improvement.fitness_improvement_threshold). Promotion requires
	// delta > threshold (strict): an experiment exactly at the bar is NOT promoted,
	// matching "improvement exceeds the threshold" in the #935 AC.
	ImprovementThreshold float64
	// RevertOnDegradation, when true, rolls back the active experiment via the
	// Reverter if fitness_after is strictly worse than baseline
	// (code_improvement.revert_on_degradation). When false a degradation is a plain
	// DISCARD (no rollback) — the shadow scoping means a degrading experiment never
	// touched real players anyway, so reverting is hygiene, not safety.
	RevertOnDegradation bool
}

// Baseline supplies the fitness BASELINE the experiment is measured against:
// the aggregate fitness of the last N REAL games (#935 "a baseline established
// from the last N real games"; #731 fitness eval). It is injected so the
// Validator's decision logic is unit-testable with a fixed baseline.
//
// PRODUCTION FOLLOW-UP (not built here): the real implementation reads recent
// real-game fitness from the #731 fitness evaluator / the gamesummary window
// (services/agent/gamesummary, gamewindow) — i.e. the same recent-real-game
// fitness the agent already aggregates — and returns its mean. An error (no real
// games yet, telemetry unreachable) makes Validate FAIL CLOSED.
type Baseline interface {
	// Fitness returns the baseline fitness for the experiment's objective(s).
	// ctx carries the validation span; an error fails the cycle closed.
	Fitness(ctx context.Context, p Proposal) (float64, error)
}

// SyntheticRunner runs the experiment through the M6 synthetic/shadow game suite
// (#774–#893) and returns a handle the FitnessMeasurer reads fitness from. It is
// injected so the Validator never blocks a unit test on a real game run.
//
// PRODUCTION FOLLOW-UP (not built here): the real implementation triggers `games`
// headless shadow games via the game coordinator (the M6 shadow-game harness,
// e.g. tests/integration/test_parallel_lifecycle.py's gameId-scoped batches but
// driven from the agent), with the experiment's flag ALREADY written shadow-scoped
// (the Writer did that). Because the experiment targets game_kind != "real", those
// synthetic games resolve the experimental variant while any concurrent real game
// is untouched — that is exactly the isolation #932 guarantees. The returned Run
// identifies the batch so the FitnessMeasurer can read its fitness.
type SyntheticRunner interface {
	// Run executes `games` synthetic games with the proposal's experiment active
	// and returns a Run handle, or an error (coordinator unreachable, games failed
	// to start) that fails the cycle closed.
	Run(ctx context.Context, p Proposal, games int) (Run, error)
}

// Run is an opaque handle to a completed synthetic-game batch, threaded from the
// SyntheticRunner to the FitnessMeasurer. Kept minimal (just a batch identity)
// so the two seams can be implemented independently; the real handle will likely
// carry the shadow game_ids / a fitness-query key.
type Run struct {
	// ID identifies the synthetic batch (e.g. a run UUID or comma-joined shadow
	// game_ids). Opaque to the Validator; meaningful to the FitnessMeasurer.
	ID string
	// Games is the number of games the runner actually completed, echoed back so
	// the measurer and the span agree on the realized sample size.
	Games int
}

// FitnessMeasurer reads the aggregate fitness of a completed synthetic Run. It is
// injected so the Validator's promote/discard logic is exercised against exact
// fitness numbers in tests.
//
// PRODUCTION FOLLOW-UP (not built here): the real implementation reads the
// shadow games' fitness from the #731 fitness evaluator (the same
// EvaluateFitness / FitnessEvaluation the decision loop produces, aggregated over
// the batch) via telemetry / the coordinator's per-game fitness, keyed by the
// Run's shadow game_ids. An error fails the cycle closed.
type FitnessMeasurer interface {
	// Measure returns the aggregate fitness of the synthetic Run. An error fails
	// the cycle closed (no promotion on an unmeasurable run).
	Measure(ctx context.Context, run Run) (float64, error)
}

// Reverter rolls back an active experiment (used only on OutcomeRevert). It is
// injected and OPTIONAL: a nil Reverter means "no rollback mechanism wired yet",
// in which case a degradation with the revert flag set still records
// OutcomeRevert on the span but performs no rollback (and notes the missing
// reverter). Separating the verdict from the rollback keeps the decision logic
// testable without a real Writer round-trip.
//
// PRODUCTION FOLLOW-UP (not built here): the real implementation removes the
// experimental variant + shadow targeting from game.json — the inverse of the
// Writer's Apply, run through the Gate so the real-resolution invariant still
// holds (removing the shadow branch only ever AFFECTS shadow games, so it is
// trivially invariant-safe, but routing it through the Writer keeps one write
// path).
type Reverter interface {
	// Revert rolls back the proposal's active experiment from the game flagset.
	// An error is recorded but does NOT change the outcome (the experiment was
	// shadow-scoped, so a failed rollback never reaches real players); the span
	// carries the error for follow-up.
	Revert(ctx context.Context, p Proposal) error
}

// Validator runs the synthetic validation cycle for an experiment Proposal and
// returns a promote/discard/revert Decision. It owns ONLY the decision logic; the
// game run, the fitness reads, and the rollback are the injected seams above, so
// the whole cycle is unit-testable with fakes and the live wiring is a documented
// follow-up.
//
// The Validator does NOT run the Gate/Writer itself: by the time Validate is
// called the experiment has ALREADY been written shadow-scoped (the Gate accepted
// it). Validate's job is to decide whether that already-shadow-active experiment
// deserves promotion to real — purely from fitness. This ordering keeps the two
// gates orthogonal: the Gate proves "this write cannot harm real players"
// (structural invariant); the Validator proves "this experiment helps shadow
// games" (fitness), and only then does M7-8 promote it to real.
type Validator struct {
	baseline Baseline
	runner   SyntheticRunner
	measurer FitnessMeasurer
	reverter Reverter // optional; nil means no rollback wired (see Reverter)
	tracer   trace.Tracer
	log      *slog.Logger
}

// NewValidator builds a Validator over the injected seams. reverter may be nil
// (no rollback mechanism wired — a revert verdict is still recorded). tracer nil
// uses the global tracer (same instrumentation scope as the rest of the agent);
// log nil uses slog.Default().
func NewValidator(baseline Baseline, runner SyntheticRunner, measurer FitnessMeasurer, reverter Reverter, tracer trace.Tracer, log *slog.Logger) *Validator {
	if tracer == nil {
		tracer = otel.Tracer(instrumentationName)
	}
	if log == nil {
		log = slog.Default()
	}
	return &Validator{
		baseline: baseline,
		runner:   runner,
		measurer: measurer,
		reverter: reverter,
		tracer:   tracer,
		log:      log,
	}
}

// Validate runs one synthetic validation cycle for p under the live cfg and
// returns the Decision. It emits code_improvement.synthetic_validation as the
// parent span (so the cycle is the issue's single trace tree: the caller's
// evaluation → invocation spans parent THIS, which parents the apply|discard
// leaf) carrying fitness_before/after/delta + the resolved flags + the outcome.
//
// Sequence:
//  1. resolve the baseline from recent real games,
//  2. run `validation_games` synthetic games with the experiment active,
//  3. measure fitness over that run,
//  4. decide: PROMOTE if (after-before) > threshold; else DISCARD; REVERT instead
//     of DISCARD when after < before AND revert_on_degradation.
//
// FAIL CLOSED: any seam error short-circuits to OutcomeDiscard with Decision.Err
// set and code_improvement.validation_error on the span — the agent NEVER
// promotes an experiment on a baseline/run/measurement it could not obtain.
func (v *Validator) Validate(ctx context.Context, p Proposal, cfg Config) Decision {
	games := cfg.ValidationGames
	if games < 1 {
		// A zero/negative game count cannot produce evidence; clamp to one game
		// rather than "validate" against an empty run (which would risk promoting
		// on no data). Logged so a misconfigured flag is visible.
		v.log.Warn("validation_games non-positive, clamping to 1", "configured", cfg.ValidationGames)
		games = 1
	}

	ctx, span := v.tracer.Start(ctx, SpanSyntheticValidation, trace.WithAttributes(
		attribute.String(AttrFlagKey, p.FlagKey),
		attribute.String(AttrRationale, p.Rationale),
		attribute.Int(AttrValidationGames, games),
		attribute.Float64(AttrImprovementThreshold, cfg.ImprovementThreshold),
	))
	defer span.End()

	// (1) Baseline from recent real games (#731 fitness). Fail closed on error:
	// without a baseline there is nothing to compare against, so the experiment
	// cannot be shown to improve anything.
	before, err := v.baseline.Fitness(ctx, p)
	if err != nil {
		return v.fail(span, games, fmt.Errorf("baseline fitness: %w", err))
	}
	span.SetAttributes(attribute.Float64(AttrFitnessBefore, before))

	// (2) Run the experiment through the synthetic suite. The experiment is
	// already written shadow-scoped (Gate-accepted), so these games resolve the
	// experimental variant while real games stay untouched (#932 isolation).
	run, err := v.runner.Run(ctx, p, games)
	if err != nil {
		return v.fail(span, games, fmt.Errorf("synthetic run: %w", err))
	}
	// Trust the runner's realized game count for the span/decision: it may have
	// completed fewer than requested (still a valid, if smaller, sample).
	if run.Games > 0 {
		games = run.Games
		span.SetAttributes(attribute.Int(AttrValidationGames, games))
	}

	// (3) Measure fitness over the synthetic run.
	after, err := v.measurer.Measure(ctx, run)
	if err != nil {
		return v.fail(span, games, fmt.Errorf("measure fitness: %w", err))
	}

	// (4) Decide on the delta.
	delta := after - before
	span.SetAttributes(
		attribute.Float64(AttrFitnessAfter, after),
		attribute.Float64(AttrFitnessDelta, delta),
	)

	dec := Decision{FitnessBefore: before, FitnessAfter: after, Games: games}
	switch {
	case delta > cfg.ImprovementThreshold:
		// Cleared the bar: this experiment earned promotion. M7-7 returns the
		// verdict; M7-8 (#936) performs the actual real-default change.
		dec.Outcome = OutcomePromote
	case after < before && cfg.RevertOnDegradation:
		// Strict degradation AND the revert flag is set: roll the experiment back.
		dec.Outcome = OutcomeRevert
		dec.Reverted = true
		v.revert(ctx, span, p)
	default:
		// Insufficient improvement (or a degradation without the revert flag):
		// discard. Nothing is promoted; the shadow experiment simply does not
		// graduate.
		dec.Outcome = OutcomeDiscard
	}

	v.recordOutcome(ctx, span, dec)
	return dec
}

// fail records a failed-closed cycle on the span and returns a discard Decision
// carrying the error. No promotion ever happens on a seam error.
func (v *Validator) fail(span trace.Span, games int, err error) Decision {
	span.SetAttributes(
		attribute.String(AttrOutcome, string(OutcomeDiscard)),
		attribute.String(AttrValidationError, err.Error()),
	)
	span.SetStatus(codes.Error, err.Error())
	v.log.Warn("code_improvement.validation failed closed",
		"outcome", string(OutcomeDiscard), "error", err)
	return Decision{Outcome: OutcomeDiscard, Games: games, Err: err}
}

// revert invokes the optional Reverter for a degradation rollback. A nil Reverter
// (no rollback wired yet) or a Revert error is recorded but never changes the
// outcome — a shadow experiment never reached real players, so a failed rollback
// is not a safety issue, only a hygiene one for the next proposal.
func (v *Validator) revert(ctx context.Context, span trace.Span, p Proposal) {
	if v.reverter == nil {
		span.SetAttributes(attribute.String(AttrValidationError, "revert requested but no reverter wired"))
		v.log.Warn("code_improvement.revert requested but no reverter wired", "flag_key", p.FlagKey)
		return
	}
	if err := v.reverter.Revert(ctx, p); err != nil {
		span.SetAttributes(attribute.String(AttrValidationError, "revert failed: "+err.Error()))
		v.log.Warn("code_improvement.revert failed", "flag_key", p.FlagKey, "error", err)
	}
}

// recordOutcome emits the apply|discard LEAF span (the issue's
// apply | discard node) under the validation span and stamps the outcome on the
// validation span itself, so a trace shows both the decision and its branch.
func (v *Validator) recordOutcome(ctx context.Context, span trace.Span, dec Decision) {
	span.SetAttributes(
		attribute.String(AttrOutcome, string(dec.Outcome)),
		attribute.Bool(AttrReverted, dec.Reverted),
	)

	// The promote branch resolves to apply (#936 performs the real change later);
	// discard and revert both resolve to the discard leaf (nothing promoted).
	leaf := SpanDiscard
	if dec.Outcome == OutcomePromote {
		leaf = SpanApply
	}
	_, leafSpan := v.tracer.Start(ctx, leaf, trace.WithAttributes(
		attribute.String(AttrOutcome, string(dec.Outcome)),
		attribute.Float64(AttrFitnessBefore, dec.FitnessBefore),
		attribute.Float64(AttrFitnessAfter, dec.FitnessAfter),
		attribute.Bool(AttrReverted, dec.Reverted),
	))
	leafSpan.End()

	v.log.Info("code_improvement.validated",
		"outcome", string(dec.Outcome),
		"fitness_before", dec.FitnessBefore,
		"fitness_after", dec.FitnessAfter,
		"games", dec.Games,
		"reverted", dec.Reverted)
}
