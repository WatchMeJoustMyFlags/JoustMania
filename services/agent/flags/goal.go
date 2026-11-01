package flags

import (
	"context"
	"errors"
	"regexp"

	"github.com/open-feature/go-sdk/openfeature"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"

	"github.com/joustmania/agent/goal"
)

// goal.go reads goal.profile (#2479, FR-201, flags.md §3.2, decide-contract.md
// §14.4): a human-owned agent-set flag the agent reads and never writes (C-034).
// Report-only: the profile reaches the game summary, the retro, the
// agent_goal_attainment metric and, when enabled, the prompt's goal lines; never
// the permission chain, the allow-list, the budgets or the rules weights.

// keyGoalProfile is goal.profile (agent set, flags.md §3).
const keyGoalProfile = "goal.profile"

// MetricGoalProfileInvalid counts goal.profile reads whose value failed the schema
// and fell back to from_objectives.
const MetricGoalProfileInvalid = "agent_goal_profile_invalid_total"

const goalMeterName = "github.com/joustmania/agent/flags"

// objectDetailer is the part of *openfeature.Client that reports the resolved
// variant: the profile's name is the goal.profile variant.
type objectDetailer interface {
	ObjectValueDetails(ctx context.Context, flag string, defaultValue any, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (openfeature.InterfaceEvaluationDetails, error)
}

// customProfileName names a valid terms variant whose variant name is unknown
// (an evaluator without details) or not of the form profileNameRe.
const customProfileName = "custom"

// profileNameRe is the variant names that may name a profile: they reach the
// prompt's goal line and a metric label, so anything else renders as custom.
var profileNameRe = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)

// SetGoalMeterProvider overrides the meter provider of
// agent_goal_profile_invalid_total (tests). Set before use.
func (f *Flags) SetGoalMeterProvider(mp otelmetric.MeterProvider) {
	if f != nil {
		f.goalInvalid = newGoalInvalidCounter(mp)
	}
}

func newGoalInvalidCounter(mp otelmetric.MeterProvider) otelmetric.Int64Counter {
	c, err := mp.Meter(goalMeterName).Int64Counter(MetricGoalProfileInvalid,
		otelmetric.WithDescription("goal.profile reads whose value failed the schema and fell back to from_objectives (#2479)"))
	if err != nil {
		c, _ = metricnoop.NewMeterProvider().Meter(goalMeterName).Int64Counter(MetricGoalProfileInvalid)
	}
	return c
}

// GoalProfile resolves one game's goal profile (decide-contract.md §14.4):
// goal.profile in the game's flag context (attrs: game_kind, experiment_id, arm;
// targeting allowed, none shipped). A terms variant is the profile, named by its
// variant. The from_objectives sentinel, a read error, a nil wrapper or client, and
// an invalid value (a WARN once and agent_goal_profile_invalid_total{reason}) all
// give the from_objectives profile: derived from the objectives weights, with the
// duration edges read from the fitness flags. A profile without terms (the mixed
// objectives) is no goal.
func (f *Flags) GoalProfile(ctx context.Context, gameID string, attrs GameAttrs) goal.Profile {
	if f == nil || f.client == nil {
		return goal.FromObjectives(defaultObjectives(), DefaultEnduranceMinSessionSeconds, DefaultAccelerateTargetSessionSeconds)
	}
	raw, variant, err := f.goalProfileValue(ctx, shieldChargeContext(gameID, attrs))
	if err != nil {
		f.log.Debug("flags goal.profile unreadable, using from_objectives", "game_id", gameID, "error", err)
		return f.goalFromObjectives(ctx)
	}
	terms, fromObjectives, perr := goal.Parse(raw)
	if perr != nil {
		reason := goal.ReasonShape
		var pe *goal.ParseError
		if errors.As(perr, &pe) {
			reason = pe.Reason
		}
		f.goalInvalidCounter().Add(ctx, 1, otelmetric.WithAttributes(attribute.String("reason", reason)))
		f.goalWarn.Do(func() {
			f.log.Warn("flags goal.profile invalid, using from_objectives", "variant", variant, "reason", reason, "error", perr)
		})
		return f.goalFromObjectives(ctx)
	}
	if fromObjectives {
		return f.goalFromObjectives(ctx)
	}
	if !profileNameRe.MatchString(variant) {
		variant = customProfileName
	}
	return goal.Profile{Name: variant, Terms: terms}
}

func (f *Flags) goalInvalidCounter() otelmetric.Int64Counter {
	f.goalInvalidOnce.Do(func() {
		if f.goalInvalid == nil {
			f.goalInvalid = newGoalInvalidCounter(otel.GetMeterProvider())
		}
	})
	return f.goalInvalid
}

// goalProfileValue reads goal.profile with its variant name when the evaluator
// reports one. The default (an evaluation that fails) is the sentinel.
func (f *Flags) goalProfileValue(ctx context.Context, evalCtx openfeature.EvaluationContext) (any, string, error) {
	def := map[string]any{goal.NameFromObjectives: true}
	if d, ok := f.client.(objectDetailer); ok {
		details, err := d.ObjectValueDetails(ctx, keyGoalProfile, def, evalCtx)
		if err != nil {
			return nil, "", err
		}
		return details.Value, details.Variant, nil
	}
	v, err := f.client.ObjectValue(ctx, keyGoalProfile, def, evalCtx)
	return v, "", err
}

// goalFromObjectives is the from_objectives profile: the objectives weights (the
// same read and fallback as the rules engine) and the two fitness thresholds.
func (f *Flags) goalFromObjectives(ctx context.Context) goal.Profile {
	return goal.FromObjectives(f.objectives(ctx),
		float64(f.intFlag(ctx, keyEnduranceMinSessionSeconds, DefaultEnduranceMinSessionSeconds)),
		float64(f.intFlag(ctx, keyAccelerateTargetSessionSecs, DefaultAccelerateTargetSessionSeconds)))
}
