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
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// Flag keys as defined in services/flagd/agent.json (flagSetId "agent").
const (
	keyEnabled                   = "enabled"
	keyMode                      = "mode"
	keyObjectives                = "objectives"
	keyModel                     = "model"
	keyPromptVariant             = "prompt_variant"
	keyInterventionsAllowed      = "interventions_allowed"
	keyBatteryThreshold          = "policy.battery_threshold"
	keyMovementVarianceWindow    = "policy.movement_variance_window"
	keyMaxInterventionsPerMinute = "policy.max_interventions_per_minute"

	// Fitness thresholds (#731). The game-objective fitness flags.
	keyEnduranceMinSessionSeconds  = "fitness.endurance.min_session_seconds"
	keyBalancedMaxSkillGap         = "fitness.balanced.max_skill_gap"
	keyBalancedSpikeSurvival       = "fitness.balanced.spike_survival_threshold"
	keyAccelerateTargetSessionSecs = "fitness.accelerate.target_session_seconds"

	// Infrastructure (Bluetooth) fitness thresholds (#735). Read every cycle so
	// they can be tuned live on stage; they drive the infra fitness evaluation.
	keyBluetoothMaxEventGapMs       = "fitness.bluetooth.max_event_gap_ms"
	keyBluetoothMaxDroppedEventsPct = "fitness.bluetooth.max_dropped_events_pct"
	keyBluetoothMinMovementUpdateHz = "fitness.bluetooth.min_movement_update_hz"

	// Lifecycle + throttle calibration flags (#766 F5). Unlike the layers above
	// these are READ ONCE AT STARTUP (main.go), not per cycle: they configure the
	// gamecontext store TTLs / eviction ticker and the decision loop's throttle,
	// which are fixed at construction. Changing them requires an agent restart.
	keyPlayerTTLSeconds     = "lifecycle.player_ttl_seconds"
	keySessionGraceSeconds  = "lifecycle.session_grace_seconds"
	keyEvictIntervalSeconds = "lifecycle.evict_interval_seconds"
	keyDecisionThrottleSecs = "decision.throttle_seconds"
)

// Safe defaults applied when flagd is unreachable or a flag is undefined.
const (
	// DefaultEnabled is fail-closed: a missing control plane means the agent
	// stays off and dispatches no interventions.
	DefaultEnabled = false
	// DefaultMode falls back to the deterministic rules path.
	DefaultMode = "rules"
	// DefaultModel is the capability-layer LLM model selection (M4 path).
	DefaultModel = "phi4-mini"
	// DefaultPromptVariant is the capability-layer prompt selection (M4 path).
	DefaultPromptVariant = "conservative"
	// DefaultBatteryThreshold (percent): player-targeted interventions are
	// blocked when the target controller's battery is below this.
	DefaultBatteryThreshold = 20
	// DefaultMovementVarianceWindow (seconds): the rolling window that
	// variance-triggered logic (#726/#731) must observe before acting.
	DefaultMovementVarianceWindow = 10
	// DefaultMaxInterventionsPerMinute is the weighted per-minute budget.
	DefaultMaxInterventionsPerMinute = 2

	// Fitness threshold defaults (#731), mirroring the services/flagd/agent.json
	// defaultVariants. Applied when flagd is unreachable or a flag is undefined.

	// DefaultEnduranceMinSessionSeconds: sessions ending earlier fail endurance
	// (fitness.endurance.min_session_seconds).
	DefaultEnduranceMinSessionSeconds = 120
	// DefaultBalancedMaxSkillGap: larger skill spreads fail balance
	// (fitness.balanced.max_skill_gap).
	DefaultBalancedMaxSkillGap = 0.4
	// DefaultBalancedSpikeSurvivalThreshold: the survival ratio a player must
	// hold through movement spikes to pass balance
	// (fitness.balanced.spike_survival_threshold).
	DefaultBalancedSpikeSurvivalThreshold = 0.8
	// DefaultAccelerateTargetSessionSeconds: sessions running past this overshoot
	// the accelerate target (fitness.accelerate.target_session_seconds).
	DefaultAccelerateTargetSessionSeconds = 60

	// Infrastructure (Bluetooth) fitness defaults (#735), mirroring the
	// services/flagd/agent.json fitness.bluetooth.* defaultVariants.

	// DefaultBluetoothMaxEventGapMs: a window event gap above this fails infra
	// fitness (fitness.bluetooth.max_event_gap_ms).
	DefaultBluetoothMaxEventGapMs = 50.0
	// DefaultBluetoothMaxDroppedEventsPct: a window drop ratio above this fails
	// infra fitness (fitness.bluetooth.max_dropped_events_pct).
	DefaultBluetoothMaxDroppedEventsPct = 0.02
	// DefaultBluetoothMinMovementUpdateHz: an update rate below this fails infra
	// fitness (fitness.bluetooth.min_movement_update_hz).
	DefaultBluetoothMinMovementUpdateHz = 10.0

	// Lifecycle + throttle defaults (#766 F5), mirroring the former hardcoded
	// constants in main.go (playerTTL/sessionGrace/evictEvery) and decision.go
	// (throttleInterval). Promotion is behavior-neutral: these defaults reproduce
	// the prior values exactly. Applied at startup when flagd is unreachable.

	// DefaultPlayerTTLSeconds: how long a silent player is retained before
	// eviction (lifecycle.player_ttl_seconds; was main.go playerTTL = 5s).
	DefaultPlayerTTLSeconds = 5.0
	// DefaultSessionGraceSeconds: how long an ended session lingers before its
	// session-scoped state resets (lifecycle.session_grace_seconds; was main.go
	// sessionGrace = 15s).
	DefaultSessionGraceSeconds = 15.0
	// DefaultEvictIntervalSeconds: how often the eviction ticker fires
	// (lifecycle.evict_interval_seconds; was main.go evictEvery = 1s).
	DefaultEvictIntervalSeconds = 1.0
	// DefaultDecisionThrottleSeconds: how often the evaluate log line and the
	// agent.disabled span are emitted (decision.throttle_seconds; was decision.go
	// throttleInterval = 1s).
	DefaultDecisionThrottleSeconds = 1.0
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
	IntValue(ctx context.Context, flag string, defaultValue int64, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (int64, error)
	FloatValue(ctx context.Context, flag string, defaultValue float64, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (float64, error)
	ObjectValue(ctx context.Context, flag string, defaultValue any, evalCtx openfeature.EvaluationContext, options ...openfeature.Option) (any, error)
}

// Capability is the capability layer: which LLM model and prompt the agent
// would use on the M4 llm path. Evaluated and recorded every cycle but not yet
// consumed (the llm path is still a fallback stub).
type Capability struct {
	// Model is the LLM model selection, e.g. "phi4-mini".
	Model string
	// PromptVariant is the prompt template selection, e.g. "conservative".
	PromptVariant string
}

// Policy is the policy half of the permission layer: the numeric constraints
// applied to decisions after the interventions_allowed allow-list gate.
type Policy struct {
	// BatteryThreshold (percent): player-targeted interventions are blocked when
	// the target controller's battery is below this value.
	BatteryThreshold int
	// MovementVarianceWindow (seconds): the rolling window variance-triggered
	// logic must observe before acting (parameterizes #726/#731 rules).
	MovementVarianceWindow int
	// MaxInterventionsPerMinute is the weighted per-minute dispatch budget.
	MaxInterventionsPerMinute int
}

// Fitness is the fitness-threshold half of the objective layer (#731): the
// per-objective success/degradation thresholds the fitness evaluator scores the
// live game context against. Evaluated every cycle (never cached) so flipping a
// threshold mid-session changes the next cycle's evaluation. chaos has no
// fitness function (it is unpredictability by definition; see README), so it has
// no threshold here.
type Fitness struct {
	// EnduranceMinSessionSeconds: sessions shorter than this fail endurance
	// (fitness.endurance.min_session_seconds, default 120).
	EnduranceMinSessionSeconds int
	// BalancedMaxSkillGap: a skill spread above this fails balance
	// (fitness.balanced.max_skill_gap, default 0.4).
	BalancedMaxSkillGap float64
	// BalancedSpikeSurvivalThreshold: the survival ratio a player must hold
	// through movement spikes to pass balance
	// (fitness.balanced.spike_survival_threshold, default 0.8).
	BalancedSpikeSurvivalThreshold float64
	// AccelerateTargetSessionSeconds: sessions past this overshoot the accelerate
	// target (fitness.accelerate.target_session_seconds, default 60).
	AccelerateTargetSessionSeconds int
}

// BluetoothFitness is the infrastructure (Bluetooth) fitness thresholds (#735):
// the transport-health limits the infra fitness evaluation scores the live
// Bluetooth context against. Evaluated every cycle (never cached) so flipping a
// threshold mid-stage changes the next infra evaluation. Kept SEPARATE from
// Fitness (game objectives) — distinct flags, distinct concerns.
type BluetoothFitness struct {
	// MaxEventGapMs: a window event gap above this fails fitness
	// (fitness.bluetooth.max_event_gap_ms, default 50).
	MaxEventGapMs float64
	// MaxDroppedEventsPct: a window drop ratio above this fails fitness
	// (fitness.bluetooth.max_dropped_events_pct, default 0.02).
	MaxDroppedEventsPct float64
	// MinMovementUpdateHz: an update rate below this fails fitness
	// (fitness.bluetooth.min_movement_update_hz, default 10).
	MinMovementUpdateHz float64
}

// Lifecycle holds the agent's lifecycle + throttle calibration values (#766 F5).
//
// These are READ ONCE AT STARTUP, not per decision cycle: they configure the
// gamecontext store TTLs, the eviction ticker, and the decision loop's log/span
// throttle, all of which are fixed at construction. A flag change here requires
// an agent restart to take effect (deliberately NOT hot-reload — the issue's
// read-at-startup decision for the store TTLs). All values are durations.
type Lifecycle struct {
	// PlayerTTL bounds how long a silent player is retained before eviction
	// (lifecycle.player_ttl_seconds, default 5s).
	PlayerTTL time.Duration
	// SessionGrace bounds how long an ended session lingers before its
	// session-scoped state resets (lifecycle.session_grace_seconds, default 15s).
	SessionGrace time.Duration
	// EvictInterval is how often the eviction ticker fires
	// (lifecycle.evict_interval_seconds, default 1s).
	EvictInterval time.Duration
	// DecisionThrottle bounds how often the evaluate log line and agent.disabled
	// span are emitted (decision.throttle_seconds, default 1s).
	DecisionThrottle time.Duration
}

// Lifecycle evaluates the read-at-startup lifecycle + throttle flags. Unlike
// Evaluate (per cycle), this is called ONCE during main()'s startup, after the
// flagd provider is registered. Each flag falls back to its safe default on any
// evaluation error (e.g. flagd not yet ready), reproducing the former hardcoded
// constants. Non-positive values fall back to the default to avoid a zero/negative
// TTL or ticker interval.
func (f *Flags) Lifecycle(ctx context.Context) Lifecycle {
	return Lifecycle{
		PlayerTTL:        f.durationFlag(ctx, keyPlayerTTLSeconds, DefaultPlayerTTLSeconds),
		SessionGrace:     f.durationFlag(ctx, keySessionGraceSeconds, DefaultSessionGraceSeconds),
		EvictInterval:    f.durationFlag(ctx, keyEvictIntervalSeconds, DefaultEvictIntervalSeconds),
		DecisionThrottle: f.durationFlag(ctx, keyDecisionThrottleSecs, DefaultDecisionThrottleSeconds),
	}
}

// durationFlag resolves a float "seconds" flag into a time.Duration, falling
// back to def seconds on any error or on a non-positive value.
func (f *Flags) durationFlag(ctx context.Context, key string, def float64) time.Duration {
	secs := f.floatFlag(ctx, key, def)
	if secs <= 0 {
		f.log.Warn("flags duration non-positive, using default", "key", key, "value", secs, "default", def)
		secs = def
	}
	return time.Duration(secs * float64(time.Second))
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
	// Capability is the capability-layer model/prompt selection.
	Capability Capability
	// InterventionsAllowed is the permission gate: only decisions whose
	// intervention appears here may be dispatched. Empty means dispatch nothing.
	InterventionsAllowed []string
	// Policy holds the numeric permission-layer constraints.
	Policy Policy
	// Fitness holds the per-objective fitness thresholds (#731).
	Fitness Fitness
	// BluetoothFitness holds the infrastructure fitness thresholds (#735).
	BluetoothFitness BluetoothFitness
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
		Enabled:    f.enabled(ctx),
		Mode:       f.mode(ctx),
		Objectives: f.objectives(ctx),
		Capability: Capability{
			Model:         f.stringFlag(ctx, keyModel, DefaultModel),
			PromptVariant: f.stringFlag(ctx, keyPromptVariant, DefaultPromptVariant),
		},
		InterventionsAllowed: f.interventionsAllowed(ctx),
		Policy: Policy{
			BatteryThreshold:          f.intFlag(ctx, keyBatteryThreshold, DefaultBatteryThreshold),
			MovementVarianceWindow:    f.intFlag(ctx, keyMovementVarianceWindow, DefaultMovementVarianceWindow),
			MaxInterventionsPerMinute: f.intFlag(ctx, keyMaxInterventionsPerMinute, DefaultMaxInterventionsPerMinute),
		},
		Fitness: Fitness{
			EnduranceMinSessionSeconds:     f.intFlag(ctx, keyEnduranceMinSessionSeconds, DefaultEnduranceMinSessionSeconds),
			BalancedMaxSkillGap:            f.floatFlag(ctx, keyBalancedMaxSkillGap, DefaultBalancedMaxSkillGap),
			BalancedSpikeSurvivalThreshold: f.floatFlag(ctx, keyBalancedSpikeSurvival, DefaultBalancedSpikeSurvivalThreshold),
			AccelerateTargetSessionSeconds: f.intFlag(ctx, keyAccelerateTargetSessionSecs, DefaultAccelerateTargetSessionSeconds),
		},
		BluetoothFitness: BluetoothFitness{
			MaxEventGapMs:       f.floatFlag(ctx, keyBluetoothMaxEventGapMs, DefaultBluetoothMaxEventGapMs),
			MaxDroppedEventsPct: f.floatFlag(ctx, keyBluetoothMaxDroppedEventsPct, DefaultBluetoothMaxDroppedEventsPct),
			MinMovementUpdateHz: f.floatFlag(ctx, keyBluetoothMinMovementUpdateHz, DefaultBluetoothMinMovementUpdateHz),
		},
	}
}

// stringFlag resolves a string flag, falling back to def on any error.
func (f *Flags) stringFlag(ctx context.Context, key, def string) string {
	v, err := f.client.StringValue(ctx, key, def, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags string fell back to default", "key", key, "error", err, "default", def)
		return def
	}
	return v
}

// intFlag resolves an integer policy flag, falling back to def on any error.
func (f *Flags) intFlag(ctx context.Context, key string, def int) int {
	v, err := f.client.IntValue(ctx, key, int64(def), openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags int fell back to default", "key", key, "error", err, "default", def)
		return def
	}
	return int(v)
}

// floatFlag resolves a float fitness flag, falling back to def on any error.
func (f *Flags) floatFlag(ctx context.Context, key string, def float64) float64 {
	v, err := f.client.FloatValue(ctx, key, def, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags float fell back to default", "key", key, "error", err, "default", def)
		return def
	}
	return v
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
