// Package flags wraps the OpenFeature SDK and exposes the agent's control-layer
// flags as typed, evaluation-time values.
//
// The agent evaluates flags from the flagd "agent" domain on EVERY decision
// cycle (issue #727) — never cached at startup — so that flipping a flag (e.g.
// the agent.enabled kill switch) takes effect with no restart.
//
// flagd merges every source file into one flat namespace keyed by the flag's
// JSON key, so the keys here are flat (`enabled`, `mode`, `objectives`,
// `interventions_allowed`) and match services/flagd/agent.json — NOT the
// dotted `agent.enabled` form used in the issue prose.
//
// All evaluation methods take a default and never return an error: when flagd
// is unreachable or a flag is undefined, the safe default applies. The defaults
// are deliberately fail-closed — enabled defaults to false and
// interventions_allowed to empty, so a missing control plane dispatches nothing.
package flags

import (
	"context"
	"log/slog"

	"github.com/open-feature/go-sdk/openfeature"
)

// Flag keys as defined in services/flagd/agent.json (flagSetId "agent").
const (
	keyEnabled              = "enabled"
	keyMode                 = "mode"
	keyObjectives           = "objectives"
	keyInterventionsAllowed = "interventions_allowed"
)

// Safe defaults applied when flagd is unreachable or a flag is undefined.
const (
	// DefaultEnabled is fail-closed: a missing control plane means the agent
	// stays off and dispatches no interventions.
	DefaultEnabled = false
	// DefaultMode falls back to the deterministic rules path.
	DefaultMode = "rules"
)

// defaultObjectives is the fallback objectives weighting. Returned as a fresh
// copy on every call so callers can never mutate the shared default.
func defaultObjectives() map[string]float64 {
	return map[string]float64{"endurance": 1.0}
}

// Evaluator is the subset of the OpenFeature client the wrapper needs. The real
// flagd-backed *openfeature.Client satisfies it, as does a client built over the
// in-memory provider in tests.
type Evaluator interface {
	BooleanValue(ctx context.Context, flag string, defaultValue bool, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (bool, error)
	StringValue(ctx context.Context, flag string, defaultValue string, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (string, error)
	ObjectValue(ctx context.Context, flag string, defaultValue any, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (any, error)
}

// Snapshot is the set of agent control flags captured for a single decision
// cycle. It is a plain value so the decision loop reasons over a consistent view.
type Snapshot struct {
	// Enabled is the kill switch. When false the decision loop short-circuits.
	Enabled bool
	// Mode selects the decision path ("rules" or "llm").
	Mode string
	// Objectives weights the session goals the rules engine optimizes for.
	Objectives map[string]float64
	// InterventionsAllowed is the permission gate: only decisions whose
	// intervention appears here may be dispatched. Empty means dispatch nothing.
	InterventionsAllowed []string
}

// Permits reports whether the named intervention is in the allow-list.
func (s Snapshot) Permits(intervention string) bool {
	for _, allowed := range s.InterventionsAllowed {
		if allowed == intervention {
			return true
		}
	}
	return false
}

// Flags evaluates the agent control flags against an OpenFeature client.
type Flags struct {
	client Evaluator
	log    *slog.Logger
}

// New builds a Flags wrapper over the given evaluator. log may be nil, in which
// case slog.Default() is used.
func New(client Evaluator, log *slog.Logger) *Flags {
	if log == nil {
		log = slog.Default()
	}
	return &Flags{client: client, log: log}
}

// Evaluate resolves all agent control flags for one decision cycle. It is called
// on every cycle (never cached) so runtime flag changes take effect immediately.
// Each flag falls back to its safe default on any evaluation error.
func (f *Flags) Evaluate(ctx context.Context) Snapshot {
	return Snapshot{
		Enabled:              f.enabled(ctx),
		Mode:                 f.mode(ctx),
		Objectives:           f.objectives(ctx),
		InterventionsAllowed: f.interventionsAllowed(ctx),
	}
}

func (f *Flags) enabled(ctx context.Context) bool {
	v, err := f.client.BooleanValue(ctx, keyEnabled, DefaultEnabled, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.enabled fell back to default", "error", err, "default", DefaultEnabled)
		return DefaultEnabled
	}
	return v
}

func (f *Flags) mode(ctx context.Context) string {
	v, err := f.client.StringValue(ctx, keyMode, DefaultMode, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.mode fell back to default", "error", err, "default", DefaultMode)
		return DefaultMode
	}
	return v
}

// objectives resolves the objectives object into map[string]float64. flagd
// returns object flags as map[string]any with numeric leaves typed as float64;
// any unparseable shape falls back to the default weighting.
func (f *Flags) objectives(ctx context.Context) map[string]float64 {
	raw, err := f.client.ObjectValue(ctx, keyObjectives, defaultObjectives(), openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.objectives fell back to default", "error", err)
		return defaultObjectives()
	}
	weights, ok := toFloatMap(raw)
	if !ok {
		f.log.Warn("flags.objectives had unexpected shape, using default", "value", raw)
		return defaultObjectives()
	}
	return weights
}

// interventionsAllowed resolves the allow-list object into []string. The flag's
// variants are JSON arrays, surfaced as []any of strings. An empty or
// unparseable result yields an empty list (dispatch nothing).
func (f *Flags) interventionsAllowed(ctx context.Context) []string {
	raw, err := f.client.ObjectValue(ctx, keyInterventionsAllowed, []any{}, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.interventions_allowed fell back to empty", "error", err)
		return nil
	}
	list, ok := toStringSlice(raw)
	if !ok {
		f.log.Warn("flags.interventions_allowed had unexpected shape, blocking all", "value", raw)
		return nil
	}
	return list
}

// toFloatMap coerces an OpenFeature object value into map[string]float64.
// Accepts the already-typed map[string]float64 (in-memory provider) and the
// map[string]any flagd shape with float64/int leaves.
func toFloatMap(raw any) (map[string]float64, bool) {
	switch m := raw.(type) {
	case map[string]float64:
		out := make(map[string]float64, len(m))
		for k, v := range m {
			out[k] = v
		}
		return out, true
	case map[string]any:
		out := make(map[string]float64, len(m))
		for k, v := range m {
			f, ok := toFloat(v)
			if !ok {
				return nil, false
			}
			out[k] = f
		}
		return out, true
	default:
		return nil, false
	}
}

func toFloat(v any) (float64, bool) {
	switch n := v.(type) {
	case float64:
		return n, true
	case float32:
		return float64(n), true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	default:
		return 0, false
	}
}

// toStringSlice coerces an OpenFeature object value into []string. Accepts an
// already-typed []string and the []any flagd shape with string leaves.
func toStringSlice(raw any) ([]string, bool) {
	switch s := raw.(type) {
	case []string:
		return append([]string(nil), s...), true
	case []any:
		out := make([]string, 0, len(s))
		for _, v := range s {
			str, ok := v.(string)
			if !ok {
				return nil, false
			}
			out = append(out, str)
		}
		return out, true
	default:
		return nil, false
	}
}
