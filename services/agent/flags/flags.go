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
	"strings"
	"time"

	"github.com/open-feature/go-sdk/openfeature"
)

// Flag keys as defined in services/flagd/agent.json (flagSetId "agent").
const (
	keyEnabled                   = "enabled"
	keyObjectives                = "objectives"
	keyInterventionsAllowed      = "interventions_allowed"
	keyBatteryThreshold          = "policy.battery_threshold"
	keyMaxInterventionsPerMinute = "policy.max_interventions_per_minute"
	// Shield limits per player per game (UC-040 A33, BR-022; #2044): operator flags
	// the agent reads and never writes (C-034). No targeting (flags.md §3), so the
	// agent's empty context resolves the coordinator's value.
	keyMaxShieldsPerPlayerPerGame = "policy.max_shields_per_player_per_game"
	keyShieldCooldownSeconds      = "policy.shield_cooldown_seconds"

	// LLM call gate (#847): the flag-controlled gate in FRONT of every remote
	// decide call (#1226) and post-game retro. Both are read EVERY cycle (never
	// cached) so tightening the budget mid-session takes effect on the very next
	// decision. #1234 retired the legacy in-game mode=llm path and its other gate
	// flags (cadence, latency budget, rules-first upgrade, context window, note).
	keyLLMEligibleGameKinds = "llm.eligible_game_kinds"
	keyLLMMaxRequestsPerMin = "llm.max_requests_per_minute"

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

	// M7-4 propose-stage flag (#931). Read live (never cached) so a retune of the
	// propose cadence takes effect on the next propose cycle — same contract as the
	// #847 llm.* gate flags. It governs how often the (expensive) propose call of
	// experiment.Proposer may fire. code_improvement.engine was retired in #1810
	// (it was recorded on the span only; D-A10).
	keyCodeImprovementMinInterval = "code_improvement.min_interval_seconds"

	// M7-8 promotion flow flags (#936). Read LIVE (never cached) so flipping the
	// mode flag takes effect on the NEXT promotion with no restart — the demo
	// moment. mode selects issue|pr|autonomous; target selects local|github. Both
	// are STRING flags read via the typed getter. The SAFE defaults (see
	// DefaultPromotionMode/Target) are baked here: mode never defaults to autonomous,
	// target never defaults to github — the safety rail.
	keyPromotionMode   = "code_improvement.mode"
	keyPromotionTarget = "code_improvement.target"

	// Lifecycle + throttle calibration flags (#766 F5, hot-reloaded since #927).
	// Unlike the per-cycle layers above, these are NOT re-read on the decision hot
	// path; they are primed once at startup and then refreshed by the OpenFeature
	// configuration-change LISTENER into a shared LifecycleHolder (holder.go), which
	// the gamecontext store TTLs / eviction ticker / decision-loop throttle read
	// live. Changing a flag takes effect with no restart and no per-cycle polling.
	keyPlayerTTLSeconds     = "lifecycle.player_ttl_seconds"
	keySessionGraceSeconds  = "lifecycle.session_grace_seconds"
	keyEvictIntervalSeconds = "lifecycle.evict_interval_seconds"
	keyDecisionThrottleSecs = "decision.throttle_seconds"

	// Experiment / cohort framework runtime knobs (#1044). These migrate the
	// runtime-tunable AGENT_EXPERIMENT_* env vars to LIVE agent.json flags so they
	// are hot-reloadable / dashboard-controllable with no container restart, exactly
	// like the kill-switch + llm.* gate flags. They are read LIVE each experiment
	// tick (experiment_loop.go) — NOT cached — so a flag flip takes effect on the
	// next tick. Since #1907 the env fallbacks are gone (FR-151 Obsolete): an
	// absent/unreadable flag falls back to its compiled default (equal to the flag
	// default), and the two gates fall back to false (closed).
	//
	// The flagd numeric-flag gotcha applies: every numeric one MUST use the typed
	// getter (a numeric flag read via ObjectValue silently TYPE_MISMATCHes to its
	// default — see llmGate). The dynamic declarer's candidate list and denylist
	// (#1907) are comma-separated STRING flags read with StringValue, not list flags,
	// so the flagd list-flag trap (a top-level JSON list read via get_object_value
	// TYPE_MISMATCHes) does not apply; the declarer parses the string.
	keyExperimentsEnabled          = "experiments_enabled"
	keyExperimentDynamicEnabled    = "experiment_dynamic_enabled"
	keyExperimentTickSeconds       = "experiment_tick_seconds"
	keyExperimentDynamicMaxConcur  = "experiment_dynamic_max_concurrent"
	keyExperimentDynamicCandidates = "experiment_dynamic_candidates"
	keyExperimentDynamicDenylist   = "experiment_dynamic_denylist"
	keyExperimentDynamicObjective  = "experiment_dynamic_objective"
	keyExperimentDynamicTargetN    = "experiment_dynamic_target_n"
	// keyExperimentSeed is the experiment seed (#1907): an OBJECT flag (a seeded
	// experiment intent, not an RNG seed) read with ObjectValue; the loop parses it.
	keyExperimentSeed              = "experiment_seed"
	keyVerdictMinN                 = "verdict_min_n"
	keyVerdictMinPairs             = "verdict_min_pairs"
	keyExperimentMaxGames          = "experiment_max_games"
	keyExperimentShadowConcurrency = "experiment_shadow_effective_concurrency"
	// #1810 (C-035, flags.md §15): the completed-game grace window is tuning (flag,
	// else compiled default; 0 = off). The proposer gate limits the agent (C-034,
	// FR-152 d): the caller ANDs it with the AGENT_EXPERIMENT_PROPOSER_ENABLED
	// ceiling, and an unresolved flag reads false (closed).
	keyExperimentCompletedGrace  = "experiment_completed_grace_seconds"
	keyExperimentProposerEnabled = "experiment_proposer_enabled"

	// Verdict-tuning float knobs migrated from env to agent.json flags (#1214),
	// finishing the cluster verdict_min_n / verdict_min_pairs started in #1044. These
	// were the AGENT_VERDICT_EFFECT_THRESHOLD / _MIN_RAW_EFFECT / _SD_FLOOR /
	// _ANCHOR_MARGIN env knobs once read at startup by the verdict constructor.
	// Migrating them makes each numeric verdict gate hot-reloadable — flip it in flagd
	// and the NEXT verdict evaluation uses the new threshold with no restart, exactly
	// like the min_n/min_pairs siblings (live-mirrored onto the Verdict via the
	// registry's Reconfigure → Verdict.SetThresholds, #1044/#1051).
	//
	// They are TUNING knobs, not safety gates: each FAILS OPEN to the current code
	// default (the same numeric value the env var defaulted to) when flagd is
	// undefined/unreachable — matching the min_n/min_pairs behavior. They are read via
	// the TYPED float getter (floatFlag/FloatValue), NEVER ObjectValue — a numeric flag
	// read via ObjectValue silently TYPE_MISMATCHes to its default (#903/#1127).
	keyVerdictEffectThreshold = "verdict_effect_threshold"
	keyVerdictMinRawEffect    = "verdict_min_raw_effect"
	keyVerdictSDFloor         = "verdict_sd_floor"
	keyVerdictAnchorMargin    = "verdict_anchor_margin"

	// Runtime SAFETY gates migrated from env to agent.json flags (#1213). These
	// were the AGENT_INTERVENTIONS_ENABLED / AGENT_ROLLOUT_ENABLED env masters read
	// ONCE at startup in main.go to select which action sink / rollout actuator was
	// wired (#730/#734). Migrating them to flags makes the gate hot-reloadable —
	// flip it in flagd and the agent stops/starts ACTUATING with no restart.
	//
	// They are SAFETY gates, not tuning knobs: unlike the #1215 game-cap migration
	// (fail-OPEN to a default), these MUST fail CLOSED. When flagd is undefined or
	// unreachable the gate resolves to the NOT-ACTING state (off), matching the
	// fail-closed env default (`:-false` in docker-compose.yml) — see DefaultInterventionsEnabled
	// / DefaultRolloutEnabled and the per-USE wiring (not cached-at-init, #1217).
	//
	// Read via the TYPED bool getter (boolFlag/BooleanValue), NOT ObjectValue — a
	// flag read via ObjectValue silently TYPE_MISMATCHes to its default (#903/#1127).
	keyInterventionsEnabled = "interventions_enabled"
	keyRolloutEnabled       = "rollout_enabled"
)

// Safe defaults applied when flagd is unreachable or a flag is undefined.
const (
	// DefaultEnabled is fail-closed: a missing control plane means the agent
	// stays off and dispatches no interventions.
	DefaultEnabled = false
	// DefaultBatteryThreshold (percent): player-targeted interventions are
	// blocked when the target controller's battery is below this.
	DefaultBatteryThreshold = 20
	// DefaultMaxInterventionsPerMinute is the weighted per-minute budget.
	DefaultMaxInterventionsPerMinute = 2
	// DefaultMaxShieldsPerPlayerPerGame and DefaultShieldCooldownSeconds are the
	// shield limits (UC-040 BR-022) when the flag cannot be read or is negative:
	// the limits stay on. They match services/flagd/agent.json and the coordinator's
	// games/base.py defaults.
	DefaultMaxShieldsPerPlayerPerGame = 3
	DefaultShieldCooldownSeconds      = 15

	// LLM call-gate default (#847), mirroring the services/flagd/agent.json
	// defaultVariant: the fail-closed safe value when flagd is unreachable.

	// DefaultLLMMaxRequestsPerMinute is the GLOBAL per-minute request cap across
	// ALL games combined plus the retro (llm.max_requests_per_minute). Each
	// admitted remote decide call or retro is one request; defaults to 6/min total.
	// The agent.json "stage" variant (30/min) is for a live remote-backend demo:
	// one game at decision.min_interval_ms=3000 asks ~15-20 times a minute
	// (#1231), and post-game retros draw on the same budget, so that is a lower
	// bound on demand. AGENT_LLM_MAX_REQUESTS_PER_MINUTE, when set, is the operator's
	// ceiling: the cap is min(env, this flag) (Loop.budgetCap, #1788, C-034), so the
	// flag can only tighten it. This default is also the lost-sync value.
	DefaultLLMMaxRequestsPerMinute = 6

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

	// M7-4 propose-stage defaults (#931), mirroring the services/flagd/agent.json
	// code_improvement.* defaultVariants. Applied when flagd is unreachable or a
	// flag is undefined.

	// DefaultCodeImprovementMinIntervalSeconds is the propose cadence floor
	// (code_improvement.min_interval_seconds): at most one experiment proposal per
	// this interval. Proposing is an LLM call, so it is far rarer than a decision
	// cycle — 300s (5 min) keeps the agent from proposing every game. Tunable live.
	DefaultCodeImprovementMinIntervalSeconds = 300.0

	// M7-8 promotion-flow defaults (#936), the SAFETY RAIL's safe flag defaults —
	// mirror the services/flagd/agent.json code_improvement.{mode,target}
	// defaultVariants. Applied when flagd is unreachable or the flag is undefined.

	// DefaultPromotionMode is "issue" — the SAFEST mode: a missing/unreachable
	// control plane opens an issue (no code/flag change), NEVER autonomous (which
	// would change the real default). The safety rail requires mode to never default
	// to autonomous.
	DefaultPromotionMode = "issue"
	// DefaultPromotionTarget is "local" — NEVER "github". A missing/unreachable
	// control plane keeps a promotion off github.com; the real GitHub path requires
	// both this flag set to github AND the explicit env gate + token (see
	// promote.NewGitHubClientFromEnv). The safety rail requires target to never
	// default to github.
	DefaultPromotionTarget = "local"

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

	// Runtime SAFETY-gate defaults (#1213), mirroring the services/flagd/agent.json
	// interventions_enabled / rollout_enabled defaultVariants AND the fail-closed
	// env default these replace (AGENT_INTERVENTIONS_ENABLED / AGENT_ROLLOUT_ENABLED,
	// both `:-false` in docker-compose.yml). These defaults are deliberately the SAFE
	// state, applied when flagd is undefined or UNREACHABLE (#1177): a missing/down
	// control plane must NOT actuate — the agent does not write interventions /
	// does not flip the rollout stage. Fail CLOSED, never fail open to acting.

	// DefaultInterventionsEnabled is fail-closed OFF: when flagd is unreachable the
	// agent's ActionSink does NOT write interventions — a decided, fully-gated
	// intervention is RECORDED/spanned but never applied (the no-op sink behavior the
	// old AGENT_INTERVENTIONS_ENABLED=false produced).
	DefaultInterventionsEnabled = false
	// DefaultRolloutEnabled is fail-closed OFF: when flagd is unreachable the rollout
	// actuator is a DRY-RUN — the infra loop still decides+spans the expansion
	// (rollout.dry_run=true) but never flips current_controller_count on the flag service
	// (the old AGENT_ROLLOUT_ENABLED=false behavior).
	DefaultRolloutEnabled = false
)

// defaultObjectives is the fallback objectives weighting. Returned as a fresh
// copy on every call so callers can never mutate the shared default.
func defaultObjectives() map[string]float64 {
	return map[string]float64{"endurance": 1.0}
}

// defaultLLMEligibleGameKinds is the fallback eligibility list for the LLM gate
// (#847). Returned as a fresh slice each call so callers can never mutate the
// shared default. Fail-closed here means defaulting to ["real"] — shadow games
// stay rules-only (#838) — NOT empty: an empty list would also be safe (it
// disables llm entirely), but ["real"] matches the agent.json schema default and
// keeps real games eligible when flagd is unreachable.
func defaultLLMEligibleGameKinds() []string {
	return []string{"real"}
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

// Policy is the policy half of the permission layer: the numeric constraints
// applied to decisions after the interventions_allowed allow-list gate.
type Policy struct {
	// BatteryThreshold (percent): player-targeted interventions are blocked when
	// the target controller's battery is below this value.
	BatteryThreshold int
	// MaxInterventionsPerMinute is the weighted per-minute dispatch budget.
	MaxInterventionsPerMinute int
	// MaxShieldsPerPlayerPerGame caps the agent's grant_shield writes per player per
	// game (UC-040 A33, BR-022). 0 = unlimited (also the zero value, so a hand-built
	// Snapshot, the rules golden among them, stays inert).
	MaxShieldsPerPlayerPerGame int
	// ShieldCooldownSeconds is the pause after the end of a player's last agent
	// shield before the next grant (UC-040 A33, BR-022). 0 = off.
	ShieldCooldownSeconds int
}

// LLMGate is the call gate in FRONT of every remote decide call and retro (#847,
// #1226), evaluated EVERY cycle (never cached) so a mid-session tightening takes
// effect on the next decision:
//
//	Eligibility — which game kinds may call a remote decide backend (EligibleGameKinds)
//	Budget      — global per-minute request cap across all games and the retro
//	              (MaxRequestsPerMinute)
//
// The per-game cadence is decision.min_interval_ms (DecisionBackend.MinInterval).
type LLMGate struct {
	// EligibleGameKinds is the set of GameContext.GameKind values that may call a
	// remote decide backend. A kind not in this list falls back to rules with
	// llm_not_eligible. Default ["real"] keeps shadow games rules-only (#838).
	EligibleGameKinds []string
	// MaxRequestsPerMinute is the GLOBAL request budget across all games combined
	// and the retro. An attempt that would exceed the cap gates with
	// llm_budget_exhausted. Default 6/min.
	MaxRequestsPerMinute int
}

// EligibleFor reports whether the given game kind may call a remote decide backend. An empty
// eligibility list admits nothing (every kind gated) — the safe reading of an
// explicitly-empty allow-list, symmetrical with InterventionsAllowed.
func (g LLMGate) EligibleFor(gameKind string) bool {
	for _, k := range g.EligibleGameKinds {
		if k == gameKind {
			return true
		}
	}
	return false
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

// Lifecycle holds the agent's lifecycle + throttle calibration values (#766 F5),
// HOT-RELOADED live since #927.
//
// These configure the gamecontext store TTLs, the eviction ticker, and the
// decision loop's log/span throttle. #766 F5 read them ONCE at startup and baked
// them into those consumers, so retuning the agent's perception/cadence needed a
// restart. #927 made them live through the OpenFeature configuration-change
// listener (the in-process resolver emits one per flag sync, #1713), and
// LifecycleHolder.Poll re-evaluates these four flags every second as the safety net
// (#1709) into a shared LifecycleHolder (holder.go) that the consumers read on their
// hot path. So a flag change here takes effect within about a second with NO restart and NO
// per-cycle read of the flag client. All values are durations.
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

// CodeImprovementConfig is the M7-4 propose-stage configuration (#931): which
// inference backend generates the experiment proposal, and the cadence floor
// between proposals. It mirrors experiment.ProposerConfig (the experiment package
// stays free of an OpenFeature dependency — the caller resolves the flags here
// and maps them, the same "package owns logic, caller owns flagd" split the
// Gate/Writer use).
type CodeImprovementConfig struct {
	// MinInterval is code_improvement.min_interval_seconds as a Duration (default
	// 300s): the propose cadence floor. <= 0 in the flag falls back to the default
	// (durationFlag guards a non-positive value).
	MinInterval time.Duration
}

// CodeImprovement evaluates the M7-4 propose-stage flags (#931) for one propose
// cycle. Read live (never cached) so a retune of the engine/cadence takes effect
// on the next cycle. Each flag falls back to its safe default on any evaluation
// error (flagd unreachable / undefined).
func (f *Flags) CodeImprovement(ctx context.Context) CodeImprovementConfig {
	return CodeImprovementConfig{
		MinInterval: f.durationFlag(ctx, keyCodeImprovementMinInterval, DefaultCodeImprovementMinIntervalSeconds),
	}
}

// PromotionConfig is the M7-8 promotion-flow configuration (#936): the live mode
// (issue|pr|autonomous) and target (local|github) the promoter acts under. It
// mirrors the promote.Config shape (the promote package stays free of an
// OpenFeature dependency — the caller resolves the flags here, adds the kill-switch
// from the Snapshot, and passes the value in: the same "package owns logic, caller
// owns flagd" split the Gate/Writer use). The SAFE defaults are applied
// on any flagd error: mode→issue (never autonomous), target→local (never github).
type PromotionConfig struct {
	// Mode is code_improvement.mode (default issue).
	Mode string
	// Target is code_improvement.target (default local).
	Target string
}

// Promotion evaluates the two M7-8 promotion-flow flags (#936) for one promotion.
// Read LIVE (never cached) so flipping the mode flag mid-session takes effect on
// the NEXT promotion with no restart — the demo moment. Each flag falls back to
// its SAFE default on any evaluation error (flagd unreachable / undefined): the
// safety rail's mode→issue / target→local defaults.
func (f *Flags) Promotion(ctx context.Context) PromotionConfig {
	return PromotionConfig{
		Mode:   f.stringFlag(ctx, keyPromotionMode, DefaultPromotionMode),
		Target: f.stringFlag(ctx, keyPromotionTarget, DefaultPromotionTarget),
	}
}

// ExperimentDefaults carries the COMPILED defaults for the experiment runtime
// knobs (#1044): each Experiment() read falls back to the matching field here when
// its agent.json flag is absent/unreadable. Since #1907 no field is env-derived
// (FR-151 Obsolete): every value equals its flag default in agent.json, pinned by
// TestCompiledDefaultsMatchFlagDefaults. The two gates (experiments_enabled,
// experiment_dynamic_enabled) have NO field here: they fall back to a literal false
// in Experiment, so a lost sync closes them whatever the caller passes (C-034).
type ExperimentDefaults struct {
	// Tick is the AllocateAndSpawn cadence (experiment_tick_seconds default, 30 s).
	Tick time.Duration
	// DynamicMaxConcurrent is the compiled default of experiment_dynamic_max_concurrent
	// (3). Since #1810 AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT is a ceiling the caller
	// applies after the read (C-034, FR-152 e), never this fallback.
	DynamicMaxConcurrent int
	// VerdictMinN is the verdict_min_n default (8).
	VerdictMinN int
	// VerdictMinPairs is the verdict_min_pairs default (5).
	VerdictMinPairs int
	// MaxGames is the compiled default of experiment_max_games (50). Since #1810
	// AGENT_EXPERIMENT_MAX_GAMES is a ceiling the caller applies after the read
	// (C-034, FR-152 f), never this fallback.
	MaxGames int
	// ShadowEffectiveConcurrency is the experiment_shadow_effective_concurrency
	// default (4).
	ShadowEffectiveConcurrency int
	// CompletedGrace is the experiment_completed_grace_seconds default (60 s,
	// #1810). 0 = off.
	CompletedGrace time.Duration
	// VerdictEffectThreshold is the verdict's standardized-effect gate
	// (#1214; was AGENT_VERDICT_EFFECT_THRESHOLD, default 0.5).
	VerdictEffectThreshold float64
	// VerdictMinRawEffect is the practical-significance floor (#1042)
	// (#1214; was AGENT_VERDICT_MIN_RAW_EFFECT, default 0.02).
	VerdictMinRawEffect float64
	// VerdictSDFloor is the minimum-SD denominator floor (#1042)
	// (#1214; was AGENT_VERDICT_SD_FLOOR, default 1e-4).
	VerdictSDFloor float64
	// VerdictAnchorMargin is the recent-real regression margin (#992)
	// (#1214; was AGENT_VERDICT_ANCHOR_MARGIN, default 0.02).
	VerdictAnchorMargin float64
}

// DefaultDynamicObjective is the compiled default of experiment_dynamic_objective
// (#1907), equal to the flag's default variant. The declarer maps any value that is
// not an experimentable objective (empty, unknown, chaos) to balanced as well.
const DefaultDynamicObjective = "balanced"

// ExperimentConfig is the LIVE-resolved experiment runtime configuration (#1044),
// re-read each experiment tick so a flag flip takes effect with no restart. Every
// field is the agent.json flag when it resolves; otherwise the matching
// ExperimentDefaults field, or false for the two gates (#1907).
type ExperimentConfig struct {
	// Enabled gates the WHOLE experiment loop (experiments_enabled). When false the
	// loop does nothing — no rehydrate-spawn, no seed, no allocate. Flipping it on
	// at runtime starts the loop; flipping it off aborts every live experiment.
	Enabled bool
	// DynamicEnabled gates the #995 heuristic declarer (experiment_dynamic_enabled).
	// When false the loop declares only the experiment seed (if any).
	DynamicEnabled bool
	// Tick is the AllocateAndSpawn cadence (experiment_tick_seconds). A non-positive
	// flag value falls back to the compiled default (durationFlag guard).
	Tick time.Duration
	// DynamicMaxConcurrent caps simultaneously-live dynamically-declared experiments
	// (experiment_dynamic_max_concurrent).
	DynamicMaxConcurrent int
	// VerdictMinN is the verdict min-N-per-arm gate (verdict_min_n).
	VerdictMinN int
	// VerdictMinPairs is the paired-verdict min-completed-pairs gate (verdict_min_pairs).
	VerdictMinPairs int
	// MaxGames is the per-experiment termination budget (experiment_max_games).
	MaxGames int
	// ShadowEffectiveConcurrency is the in-flight shadow-spawn concurrency cap
	// (experiment_shadow_effective_concurrency).
	ShadowEffectiveConcurrency int
	// VerdictEffectThreshold is the live standardized-effect gate (verdict_effect_threshold,
	// #1214): the minimum |Cohen's d| / |d_z| for a promote/discard verdict.
	VerdictEffectThreshold float64
	// VerdictMinRawEffect is the live practical-significance floor (verdict_min_raw_effect,
	// #1214): the minimum RAW effect magnitude a promote/discard requires (#1042).
	VerdictMinRawEffect float64
	// VerdictSDFloor is the live minimum-SD denominator floor (verdict_sd_floor, #1214):
	// floors the d_z / Cohen's d denominator so the standardized stat stays finite (#1042).
	VerdictSDFloor float64
	// VerdictAnchorMargin is the live recent-real regression margin (verdict_anchor_margin,
	// #1214): a PROMOTE is downgraded only when the experimental arm regresses below the
	// recent-real baseline by MORE than this (#992).
	VerdictAnchorMargin float64
	// CompletedGrace is the #1020 bounded-grace window for a completed shadow game
	// (experiment_completed_grace_seconds, #1810). 0 or less = off.
	CompletedGrace time.Duration
	// DynamicCandidates is the raw comma-separated candidate list of the dynamic
	// declarer (experiment_dynamic_candidates, #1907); "" = every tunable flag. The
	// declarer parses it and always filters it through the built-in denylist.
	DynamicCandidates string
	// DynamicDenylist is the raw comma-separated list of keys the dynamic declarer
	// ADDS to its built-in denylist (experiment_dynamic_denylist, #1907); it can
	// never remove a built-in key (UC-070 BR-008, C-034). "" or unresolved = only
	// the built-in list.
	DynamicDenylist string
	// DynamicObjective is the declarer's default objective
	// (experiment_dynamic_objective, #1907); unresolved = DefaultDynamicObjective.
	DynamicObjective string
	// DynamicTargetN is the games-per-arm of a dynamically declared experiment
	// (experiment_dynamic_target_n, #1907); unresolved = 0 (the registry
	// reconciles it up to the verdict min-N).
	DynamicTargetN int
	// Seed is the raw resolved experiment_seed object (#1907): {} = no seed, else
	// the flags.md §3 schema, which the experiment loop validates
	// (parseExperimentSeed). nil = the flag could not be resolved: no seed, and
	// nothing latched.
	Seed any
	// ProposerEnabled is the flag half of the proposer gate
	// (experiment_proposer_enabled, #1810). The caller ANDs it with the
	// AGENT_EXPERIMENT_PROPOSER_ENABLED ceiling (C-034, FR-152 d). It never falls
	// back to the env: an unresolved flag reads false (closed).
	ProposerEnabled bool
}

// Experiment evaluates the migrated experiment runtime knobs (#1044, #1214) for one
// experiment tick. Read LIVE (never cached) so a hot-reload of any knob takes
// effect on the next tick. Each tuning flag falls back to the matching
// ExperimentDefaults field (the compiled default, equal to the flag default) on any
// evaluation error or flag absence. The two gates and the proposer gate fall back to
// a literal false: an unresolved gate is closed, with no env fallback (#1907, FR-150,
// C-034). Numeric flags use the TYPED getters (the flagd
// numeric-flag gotcha — see llmGate); the duration uses durationFlag (which floors
// a non-positive value at the default).
func (f *Flags) Experiment(ctx context.Context, def ExperimentDefaults) ExperimentConfig {
	return ExperimentConfig{
		Enabled:                    f.boolFlag(ctx, keyExperimentsEnabled, false),
		DynamicEnabled:             f.boolFlag(ctx, keyExperimentDynamicEnabled, false),
		Tick:                       f.durationFlag(ctx, keyExperimentTickSeconds, def.Tick.Seconds()),
		DynamicMaxConcurrent:       f.intFlag(ctx, keyExperimentDynamicMaxConcur, def.DynamicMaxConcurrent),
		VerdictMinN:                f.intFlag(ctx, keyVerdictMinN, def.VerdictMinN),
		VerdictMinPairs:            f.intFlag(ctx, keyVerdictMinPairs, def.VerdictMinPairs),
		MaxGames:                   f.intFlag(ctx, keyExperimentMaxGames, def.MaxGames),
		ShadowEffectiveConcurrency: f.intFlag(ctx, keyExperimentShadowConcurrency, def.ShadowEffectiveConcurrency),
		// Verdict-tuning float knobs (#1214). Typed float getter (the flagd numeric-flag
		// gotcha — see llmGate); fail-OPEN to the compiled default (the verdict's code
		// default) on any error/absence, exactly like the min_n/min_pairs siblings above.
		VerdictEffectThreshold: f.floatFlag(ctx, keyVerdictEffectThreshold, def.VerdictEffectThreshold),
		VerdictMinRawEffect:    f.floatFlag(ctx, keyVerdictMinRawEffect, def.VerdictMinRawEffect),
		VerdictSDFloor:         f.floatFlag(ctx, keyVerdictSDFloor, def.VerdictSDFloor),
		VerdictAnchorMargin:    f.floatFlag(ctx, keyVerdictAnchorMargin, def.VerdictAnchorMargin),
		// #1810: grace is tuning (flag, else 60 s; 0 = off, so not durationFlag,
		// which floors a non-positive value). The proposer gate's def is false: a
		// lost sync closes it (C-034).
		CompletedGrace:  time.Duration(f.intFlag(ctx, keyExperimentCompletedGrace, int(def.CompletedGrace/time.Second))) * time.Second,
		ProposerEnabled: f.boolFlag(ctx, keyExperimentProposerEnabled, false),
		// #1907: the dynamic declarer settings. Unresolved = the compiled defaults
		// (the flag defaults); an empty denylist still means the built-in list,
		// which the declarer applies whatever this resolves to (UC-070 BR-008).
		DynamicCandidates: f.stringFlag(ctx, keyExperimentDynamicCandidates, ""),
		DynamicDenylist:   f.stringFlag(ctx, keyExperimentDynamicDenylist, ""),
		DynamicObjective:  f.stringFlag(ctx, keyExperimentDynamicObjective, DefaultDynamicObjective),
		DynamicTargetN:    f.intFlag(ctx, keyExperimentDynamicTargetN, 0),
		Seed:              f.experimentSeed(ctx),
	}
}

// experimentSeed resolves the raw experiment_seed object (#1907), or nil when it
// cannot be resolved (flagd unreachable, flag missing, no first sync): an
// unresolved seed declares nothing.
func (f *Flags) experimentSeed(ctx context.Context) any {
	v, err := f.client.ObjectValue(ctx, keyExperimentSeed, nil, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags experiment_seed unresolved: no seed", "error", err)
		return nil
	}
	return v
}

// ExperimentProposerEnabled resolves only experiment_proposer_enabled (#1810), the
// flag half of the proposer gate the M7-4 proposer reads at each game end. An
// unresolved flag reads false (closed, C-034).
func (f *Flags) ExperimentProposerEnabled(ctx context.Context) bool {
	return f.boolFlag(ctx, keyExperimentProposerEnabled, false)
}

// boolFlag resolves a boolean flag, falling back to def on any error.
func (f *Flags) boolFlag(ctx context.Context, key string, def bool) bool {
	v, err := f.client.BooleanValue(ctx, key, def, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags bool fell back to default", "key", key, "error", err, "default", def)
		return def
	}
	return v
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
	// Objectives weights the session goals the rules engine optimizes for.
	Objectives map[string]float64
	// InterventionsAllowed is the permission gate: only decisions whose
	// intervention appears here may be dispatched. Empty means dispatch nothing.
	InterventionsAllowed []string
	// Policy holds the numeric permission-layer constraints.
	Policy Policy
	// LLMGate holds the LLM call gate (#847): eligibility and the global
	// per-minute request budget.
	LLMGate LLMGate
	// Fitness holds the per-objective fitness thresholds (#731).
	Fitness Fitness
	// BluetoothFitness holds the infrastructure fitness thresholds (#735).
	BluetoothFitness BluetoothFitness
	// Decision holds the decision-backend flags (#1226): which backend answers
	// the decide contract, the act-confidence threshold, content capture, and
	// each remote backend's model / base URL / timeout. Read every cycle.
	Decision DecisionBackend
	// InterventionsEnabled is the live interventions_enabled actuation gate
	// (fail-closed false), carried so agent.flag_state shows a write gate that is
	// off while the agent is enabled (#2093). The write path reads the gate itself
	// at use time (GatedActionSink); this copy is for observability only.
	InterventionsEnabled bool
	// ProbeDecisions is decision.probe_decisions (#1810, C-035): when true the
	// loop swaps the rules engine for the probe engine (synthetic noop only),
	// except in pattern games. Read every cycle.
	ProbeDecisions bool
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
	// rollout evaluates the rollout set (remediation_allowed): its own in-process
	// provider and selector (#1713). nil: client serves it too (tests).
	rollout Evaluator
	// game evaluates the game set, read-only: only the two shield flags of the
	// charge ledger (ShieldCharge, UC-040 A31). nil: ShieldCharge returns its
	// defaults, never a read through client.
	game     Evaluator
	gameWarn gameReadWarn
	// gameModes keeps the first shield.activation read per game, so a `hold` read
	// follows the coordinator's per-game latch (UC-040 A31, BR-027, #2096).
	gameModes gameModeLatch
	// gameAttrs returns a game's kind, experiment id and arm as the agent has seen
	// them (main.go wires the Multiplexer); nil means a real game (#2096).
	gameAttrs func(gameID string) GameAttrs
	// now is the clock of gameModes; nil means time.Now (tests inject one).
	now func() time.Time
	log *slog.Logger
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
		Objectives:           f.objectives(ctx),
		InterventionsAllowed: f.interventionsAllowed(ctx),
		Policy: Policy{
			BatteryThreshold:          f.intFlag(ctx, keyBatteryThreshold, DefaultBatteryThreshold),
			MaxInterventionsPerMinute: f.intFlag(ctx, keyMaxInterventionsPerMinute, DefaultMaxInterventionsPerMinute),
			MaxShieldsPerPlayerPerGame: f.nonNegativeIntFlag(ctx, keyMaxShieldsPerPlayerPerGame,
				DefaultMaxShieldsPerPlayerPerGame),
			ShieldCooldownSeconds: f.nonNegativeIntFlag(ctx, keyShieldCooldownSeconds, DefaultShieldCooldownSeconds),
		},
		LLMGate: f.llmGate(ctx),
		Fitness: Fitness{
			EnduranceMinSessionSeconds:     f.intFlag(ctx, keyEnduranceMinSessionSeconds, DefaultEnduranceMinSessionSeconds),
			BalancedMaxSkillGap:            f.floatFlag(ctx, keyBalancedMaxSkillGap, DefaultBalancedMaxSkillGap),
			BalancedSpikeSurvivalThreshold: f.floatFlag(ctx, keyBalancedSpikeSurvival, DefaultBalancedSpikeSurvivalThreshold),
			AccelerateTargetSessionSeconds: f.intFlag(ctx, keyAccelerateTargetSessionSecs, DefaultAccelerateTargetSessionSeconds),
		},
		BluetoothFitness: f.BluetoothFitness(ctx),
		Decision:         f.DecisionBackend(ctx),
		ProbeDecisions:   f.probeDecisions(ctx),

		InterventionsEnabled: f.InterventionsEnabled(ctx),
	}
}

// BluetoothFitness evaluates ONLY the three fitness.bluetooth.* thresholds
// (#735). It is the narrow accessor the infrastructure loop reads on every
// observe cycle (~1Hz) so the Bluetooth fitness thresholds are tunable live on
// stage with no restart — without paying for a full four-layer Evaluate the
// infra path never consumes. Each flag falls back to its safe default on any
// evaluation error (e.g. flagd unreachable), so a down control plane reverts to
// the flagd-schema defaults rather than failing the cycle.
func (f *Flags) BluetoothFitness(ctx context.Context) BluetoothFitness {
	return BluetoothFitness{
		MaxEventGapMs:       f.floatFlag(ctx, keyBluetoothMaxEventGapMs, DefaultBluetoothMaxEventGapMs),
		MaxDroppedEventsPct: f.floatFlag(ctx, keyBluetoothMaxDroppedEventsPct, DefaultBluetoothMaxDroppedEventsPct),
		MinMovementUpdateHz: f.floatFlag(ctx, keyBluetoothMinMovementUpdateHz, DefaultBluetoothMinMovementUpdateHz),
	}
}

// InterventionsEnabled resolves the runtime intervention-actuation gate (#1213,
// interventions_enabled). It is the migrated AGENT_INTERVENTIONS_ENABLED env
// master: when true the agent's ActionSink WRITES decisions to the flag service (interventions set);
// when false (or flagd unreachable) it records/spans the decision but applies
// nothing. Read at USE-TIME (per Apply), NOT cached at startup — flipping the flag
// in flagd starts/stops actuation with no restart, and a flagd outage at any moment
// FAILS CLOSED (DefaultInterventionsEnabled=false) so the agent never writes when it
// cannot confirm the gate is on (#1177). Distinct from the `enabled` kill switch
// (which gates whether the loop DECIDES) and from interventions_allowed (which gates
// WHICH intervention types may dispatch): this gates whether a fully-permitted
// decision is actually APPLIED to disk.
func (f *Flags) InterventionsEnabled(ctx context.Context) bool {
	return f.boolFlag(ctx, keyInterventionsEnabled, DefaultInterventionsEnabled)
}

// RemediationAllowed evaluates the rollout set's remediation_allowed flag
// through flagd (#1411, R1), on the rollout domain's provider (selector
// flagSetId=rollout, #1713). Any error resolves false: auto-rollback stays off
// (fail closed).
func (f *Flags) RemediationAllowed(ctx context.Context) bool {
	client := f.client
	if f.rollout != nil {
		client = f.rollout
	}
	v, err := client.BooleanValue(ctx, "remediation_allowed", false, openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags remediation_allowed fell back to default", "error", err, "default", false)
		return false
	}
	return v
}

// RolloutEnabled resolves the runtime rollout-actuation gate (#1213,
// rollout_enabled). It is the migrated AGENT_ROLLOUT_ENABLED env master: when true
// the infra loop's actuator FLIPS current_controller_count on the flag service (rollout set); when
// false (or flagd unreachable) it is a DRY-RUN — the loop decides+spans the
// expansion (rollout.dry_run=true) but writes nothing. Read at USE-TIME (per
// SetControllerCount / DryRun call), NOT cached at startup — flipping the flag in
// flagd starts/stops applying with no restart, and a flagd outage at any moment
// FAILS CLOSED (DefaultRolloutEnabled=false) so the agent never flips the rollout
// stage when it cannot confirm the gate is on (#1177).
func (f *Flags) RolloutEnabled(ctx context.Context) bool {
	return f.boolFlag(ctx, keyRolloutEnabled, DefaultRolloutEnabled)
}

// AgentEnabled resolves ONLY the `enabled` kill switch, without the full Evaluate
// snapshot. It is the narrow USE-TIME read the post-game experiment proposer makes
// at each game end (#1270). It FAILS CLOSED (DefaultEnabled=false) when flagd is
// unreachable, exactly like the decision loop's read.
func (f *Flags) AgentEnabled(ctx context.Context) bool {
	return f.enabled(ctx)
}

// ExperimentsEnabled resolves ONLY the `experiments_enabled` gate (#1270). It fails
// CLOSED (false) when the flag is absent or flagd is unreachable, the same fallback
// Experiment uses (#1907: no env fallback), so the post-game proposer and the
// experiment loop always agree on whether experiments are on.
func (f *Flags) ExperimentsEnabled(ctx context.Context) bool {
	return f.boolFlag(ctx, keyExperimentsEnabled, false)
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

// nonNegativeIntFlag is intFlag for a limit where a negative value is a mistake: it
// gives def, like a read error, so the limit stays on (UC-040 BR-022).
func (f *Flags) nonNegativeIntFlag(ctx context.Context, key string, def int) int {
	v := f.intFlag(ctx, key, def)
	if v < 0 {
		f.log.Debug("flags negative limit fell back to default", "key", key, "value", v, "default", def)
		return def
	}
	return v
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

// interventionsAllowed resolves the allow-list into []string. The flag is a
// STRING flag whose variants are comma-separated intervention ids (e.g.
// "play_audio_cue,grant_shield"); `none` is the empty string. It is deliberately
// NOT a LIST/object flag: top-level LIST flags hit the flagd RPC resolver's
// get_object_value TYPE_MISMATCH and silently fall back to the passed default —
// for an allow-list that default is empty, so the gate would block EVERY
// intervention even when a non-`none` variant is active (#1127). A STRING flag is
// read cleanly by the RPC resolver, so the agent honors the live variant; the
// game_coordinator (in-process resolver) reads the same string and parses it
// identically. An empty or unreadable value yields an empty list (dispatch
// nothing) — fail-closed.
func (f *Flags) interventionsAllowed(ctx context.Context) []string {
	raw, err := f.client.StringValue(ctx, keyInterventionsAllowed, "", openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.interventions_allowed fell back to empty", "error", err)
		return nil
	}
	return parseCSVList(raw)
}

// parseCSVList splits a comma-separated flag value into a slice of trimmed,
// non-empty tokens. The empty string (the `none` variant) yields nil — an empty
// allow-list, the fail-closed default.
func parseCSVList(raw string) []string {
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if s := strings.TrimSpace(p); s != "" {
			out = append(out, s)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// llmGate resolves the three LLM-call-gate flags (#847) for one cycle. Each falls
// back to its safe default on any error, so a down control plane gates the llm
// path conservatively rather than firing uncontrolled.
//
// flagd gotcha: the numeric flags must use the TYPED getters. A numeric flag read
// through ObjectValue resolves as a silent TYPE_MISMATCH default (the in-process
// resolver returns the flag's typed value only via the matching getter), so the
// interval goes through floatFlag (float seconds -> time.Duration, reusing the
// durationFlag helper) and the budget through intFlag.
func (f *Flags) llmGate(ctx context.Context) LLMGate {
	return LLMGate{
		EligibleGameKinds:    f.eligibleGameKinds(ctx),
		MaxRequestsPerMinute: f.intFlag(ctx, keyLLMMaxRequestsPerMin, DefaultLLMMaxRequestsPerMinute),
	}
}

// eligibleGameKinds resolves the llm.eligible_game_kinds allow-list into []string.
// The flag is a STRING flag whose variants are comma-separated game kinds (e.g.
// "real,shadow"); `none` is the empty string. It is deliberately NOT a LIST/array
// flag: array-valued variants are rejected by flagd's JSON schema
// (`got array, want boolean`), so the whole flag fails to load and the agent
// silently falls back to the default — exactly the #1221 bug (same class as the
// interventions_allowed #1127 fix). A STRING flag is read cleanly by the RPC
// resolver, so the live variant is honored. On a resolution error it falls back to
// the default ["real"] — fail-closed to shadow-games-rules-only, NOT to empty,
// which would disable llm entirely. The empty string (`none` variant) is a valid
// intentional state and yields an empty list (llm disabled), distinct from an error.
func (f *Flags) eligibleGameKinds(ctx context.Context) []string {
	raw, err := f.client.StringValue(ctx, keyLLMEligibleGameKinds, "real", openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.llm.eligible_game_kinds fell back to default", "error", err)
		return defaultLLMEligibleGameKinds()
	}
	return parseCSVList(raw)
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
