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
	keyMode                      = "mode"
	keyObjectives                = "objectives"
	keyModel                     = "model"
	keyPromptVariant             = "prompt_variant"
	keyInterventionsAllowed      = "interventions_allowed"
	keyBatteryThreshold          = "policy.battery_threshold"
	keyMovementVarianceWindow    = "policy.movement_variance_window"
	keyMaxInterventionsPerMinute = "policy.max_interventions_per_minute"

	// LLM call gate (#847). The three-layer, flag-controlled gate in FRONT of
	// every LLM decision attempt (#739), so the llm path can never burn tokens
	// uncontrolled. All three are read EVERY cycle (never cached) so tightening
	// the budget mid-session takes effect on the very next decision.
	keyLLMEligibleGameKinds   = "llm.eligible_game_kinds"
	keyLLMMinDecisionInterval = "llm.min_decision_interval_seconds"
	keyLLMMaxRequestsPerMin   = "llm.max_requests_per_minute"
	// keyLLMLatencyBudget bounds a single async inference call (#917). Read every
	// cycle like the other gate flags so the budget can be tuned live on stage.
	keyLLMLatencyBudget = "llm.latency_budget_seconds"

	// In-game rules-first async upgrade (#1207). Default behavior for mode="llm" is
	// now RULES-FIRST/immediate; the in-game LLM is demoted to an explicit,
	// length-gated async UPGRADE layered UNDER mode="llm" (no third mode). Read every
	// cycle like the other gate flags so the switch and the estimate parameters can be
	// tuned live on stage. The two numeric flags MUST use the TYPED getters — a numeric
	// flag read via ObjectValue silently TYPE_MISMATCHes to its default (see the gotcha
	// at llmGate).
	//
	//	keyLLMInGameAsyncUpgrade — master switch (bool, default off). When off, mode="llm"
	//	    short-circuits to rules every in-game cycle: no async LLM is ever fired in game.
	//	keyLLMSecondsPerPlayer   — per-player time budget for the length estimate (number).
	//	keyLLMStyleFactor        — "balanced<->aggressive" knob scaling the expected length
	//	    (number), shifting how often the LLM upgrade gets to weigh in.
	keyLLMInGameAsyncUpgrade = "llm.in_game_async_upgrade"
	keyLLMSecondsPerPlayer   = "llm.seconds_per_player"
	keyLLMStyleFactor        = "llm.style_factor"

	// M7-2 rolling game context window (#929). How many recent game summaries the
	// agent injects into EVERY llm prompt as the cross-game narrative context block.
	// Read EVERY cycle (never cached) on the decision hot path — exactly like the
	// #847 gate flags above — so retuning N on stage takes effect on the very next
	// llm call with no restart. The decision path clamps the value to
	// [0, gamewindow.RetentionCap].
	keyLLMContextGames = "llm.context_games"

	// M7-3 operator prompt context note (#930). A FREE-TEXT note an operator appends
	// to the agent's system prompt at runtime via flagd, with no redeploy. Flat key
	// matching the repo convention (mode, llm.context_games, policy.battery_threshold);
	// the "agent." in the issue title is the flagd DOMAIN, not part of the key. Read
	// EVERY cycle like context_games above (never cached) so changing the note on stage
	// takes effect on the very next llm call. The decision path VALIDATES + length-bounds
	// the value (llm.ValidateContextNote) before it reaches the model; an invalid/empty/
	// oversized value injects nothing. Default empty = no operator section.
	keyPromptContextNote = "prompt.context_note"

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

	// M7-7 synthetic validation gate flags (#935). Read live (never cached) so a
	// stage retune of the validation cadence/bar takes effect on the next
	// validation cycle — same contract as the #847 llm.* numeric gate flags, and
	// the numeric ones use the TYPED getters (the flagd numeric-flag gotcha: a
	// numeric flag read via ObjectValue silently TYPE_MISMATCHes to its default —
	// see llmGate). They govern experiment.Validator: how many synthetic/shadow
	// games to validate an experiment against, the fitness improvement required to
	// promote it, and whether a degrading experiment is rolled back.
	keyValidationGames             = "code_improvement.validation_games"
	keyFitnessImprovementThreshold = "code_improvement.fitness_improvement_threshold"
	keyRevertOnDegradation         = "code_improvement.revert_on_degradation"

	// M7-4 propose-stage flags (#931). Read live (never cached) so a retune of the
	// propose engine / cadence takes effect on the next propose cycle — same
	// contract as the #847 llm.* gate flags and the #935 validation flags. They
	// govern experiment.Proposer: which inference backend generates the proposal,
	// and how often the (expensive) propose call may fire.
	keyCodeImprovementEngine      = "code_improvement.engine"
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
	// next tick. The ENV value (#1041/#1043 bootstrap passthrough) stays the
	// DEFAULT each read falls back to when the flag is absent/unreadable: flag
	// overrides, env is the floor (fail-safe, exactly the kill-switch pattern).
	//
	// The flagd numeric-flag gotcha applies: every numeric one MUST use the typed
	// getter (a numeric flag read via ObjectValue silently TYPE_MISMATCHes to its
	// default — see llmGate). The two LIST-valued declarer knobs
	// (AGENT_EXPERIMENT_DYNAMIC_CANDIDATES / _DENYLIST) deliberately STAY as env:
	// top-level LIST flags hit the same get_object_value TYPE_MISMATCH and only the
	// in-process resolver (port 8015) reads them, so a list flag here would be
	// silently broken (the issue permits keeping those two as env).
	keyExperimentsEnabled          = "experiments_enabled"
	keyExperimentDynamicEnabled    = "experiment_dynamic_enabled"
	keyExperimentTickSeconds       = "experiment_tick_seconds"
	keyExperimentDynamicMaxConcur  = "experiment_dynamic_max_concurrent"
	keyVerdictMinN                 = "verdict_min_n"
	keyVerdictMinPairs             = "verdict_min_pairs"
	keyExperimentMaxGames          = "experiment_max_games"
	keyExperimentShadowConcurrency = "experiment_shadow_effective_concurrency"

	// Verdict-tuning float knobs migrated from env to agent.json flags (#1214),
	// finishing the cluster verdict_min_n / verdict_min_pairs started in #1044. These
	// were the AGENT_VERDICT_EFFECT_THRESHOLD / _MIN_RAW_EFFECT / _SD_FLOOR /
	// _ANCHOR_MARGIN env knobs read ONCE at startup by experiment.NewVerdictFromEnv.
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

	// LLM call-gate defaults (#847), mirroring the services/flagd/agent.json
	// defaultVariants. These are the fail-closed safe values when flagd is
	// unreachable: a missing control plane gates the llm path conservatively
	// rather than letting it fire uncontrolled.

	// DefaultLLMMinDecisionIntervalSeconds is the per-game cadence floor: at most
	// one LLM attempt per game per this interval (llm.min_decision_interval_seconds).
	DefaultLLMMinDecisionIntervalSeconds = 10.0
	// DefaultLLMMaxRequestsPerMinute is the GLOBAL per-minute request cap across
	// ALL games combined (llm.max_requests_per_minute). Each admitted LLM attempt
	// is one request; defaults to 6/min total regardless of game count.
	DefaultLLMMaxRequestsPerMinute = 6
	// DefaultLLMLatencyBudgetSeconds bounds a single async inference call (#917):
	// an in-flight result not produced within this many seconds is dropped outright
	// (context deadline) and the cycle's deterministic rules fallback decides
	// instead, so "the system always decides" holds without the loop ever blocking.
	// 8s sits inside the 2-10s inference envelope from the issue — long enough for a
	// healthy backend to answer, short enough that a hung tier degrades to rules
	// within one decision interval.
	DefaultLLMLatencyBudgetSeconds = 8.0

	// In-game rules-first async upgrade defaults (#1207), mirroring the
	// services/flagd/agent.json defaultVariants. Applied when flagd is unreachable or a
	// flag is undefined.

	// DefaultLLMInGameAsyncUpgrade is the master-switch default: OFF. A fresh
	// deployment or an unreachable flagd keeps mode="llm" RULES-FIRST in game and never
	// fires the async LLM upgrade — the operator opts in by setting the flag. This is
	// fail-closed to the immediate, deterministic rules path.
	DefaultLLMInGameAsyncUpgrade = false
	// DefaultLLMSecondsPerPlayer is the per-player time budget for the length estimate
	// (llm.seconds_per_player). expected_seconds = ActivePlayerCount × seconds_per_player
	// × style_factor; the async LLM upgrade fires only when expected_seconds is at least
	// the latency budget (default 8s). At 3s/player the cross-over is ~3 players at
	// style_factor 1.0, so a 2p/6s shadow stays rules-only while an 8p endurance game
	// earns the upgrade.
	DefaultLLMSecondsPerPlayer = 3.0
	// DefaultLLMStyleFactor scales the expected length (llm.style_factor), the
	// "balanced<->aggressive" knob: >1 lengthens the estimate so the LLM weighs in on
	// shorter games, <1 shortens it so only longer games qualify. 1.0 is neutral.
	DefaultLLMStyleFactor = 1.0

	// DefaultLLMContextGames is the M7-2 cross-game context window size (#929): how
	// many recent game summaries to inject into every llm prompt when flagd is
	// unreachable or the flag is undefined. 3 mirrors the agent.json defaultVariant
	// — a few games of memory, well under gamewindow.RetentionCap.
	DefaultLLMContextGames = 3
	// DefaultPromptContextNote is the M7-3 operator note default (#930): EMPTY, so a
	// fresh deployment or an unreachable flagd injects NO operator-context section and
	// the prompt is exactly the hardcoded base. The operator opts in by setting the
	// flag; absence is the safe no-op (fail-closed to "no note").
	DefaultPromptContextNote = ""

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

	// M7-7 synthetic validation defaults (#935), mirroring the
	// services/flagd/agent.json code_improvement.* defaultVariants. Applied when
	// flagd is unreachable or a flag is undefined.

	// DefaultValidationGames is how many synthetic/shadow games an experiment is
	// validated against (code_improvement.validation_games). 5 matches the #935
	// suggested default — enough games to smooth single-game variance, few enough
	// to keep a validation cycle cheap.
	DefaultValidationGames = 5
	// DefaultFitnessImprovementThreshold is the minimum fitness improvement
	// (after-before) an experiment must EXCEED to be promoted
	// (code_improvement.fitness_improvement_threshold). 0.05 (a 5% fitness-point
	// improvement on the 0..1 progress scale, see decision/fitness.go) is a
	// conservative bar: a noisy near-zero delta does not graduate. Tunable live.
	DefaultFitnessImprovementThreshold = 0.05
	// DefaultRevertOnDegradation is fail-safe-leaning: roll back an experiment that
	// degrades shadow fitness (code_improvement.revert_on_degradation). Shadow
	// scoping means a degrading experiment never reached real players, so this is
	// hygiene rather than safety, but defaulting it on keeps the shadow surface
	// clean for the next proposal.
	DefaultRevertOnDegradation = true

	// M7-4 propose-stage defaults (#931), mirroring the services/flagd/agent.json
	// code_improvement.* defaultVariants. Applied when flagd is unreachable or a
	// flag is undefined.

	// DefaultCodeImprovementEngine selects the propose-stage inference backend
	// (code_improvement.engine). "stub" is the only meaningful value today — the
	// production endpointBackend.Infer is a sentinel until a real Ollama/cloud
	// transport lands (#738/#742), so a non-stub value still degrades to
	// no-proposal. Fail-safe: a missing control plane keeps the agent on the
	// inert stub rather than dialing a backend it cannot reach.
	DefaultCodeImprovementEngine = "stub"
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
	// control plane must NOT actuate — the agent does not write interventions.json /
	// does not flip the rollout stage. Fail CLOSED, never fail open to acting.

	// DefaultInterventionsEnabled is fail-closed OFF: when flagd is unreachable the
	// agent's ActionSink does NOT write interventions.json — a decided, fully-gated
	// intervention is RECORDED/spanned but never applied (the no-op sink behavior the
	// old AGENT_INTERVENTIONS_ENABLED=false produced).
	DefaultInterventionsEnabled = false
	// DefaultRolloutEnabled is fail-closed OFF: when flagd is unreachable the rollout
	// actuator is a DRY-RUN — the infra loop still decides+spans the expansion
	// (rollout.dry_run=true) but never flips current_controller_count in rollout.json
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

// LLMGate is the three-layer call gate in FRONT of every LLM decision attempt
// (#847), evaluated EVERY cycle (never cached) so a mid-session tightening takes
// effect on the next decision. The gate sits before the #739 prompt build/capture:
// when it denies, the cycle records the layer's fallback_reason and does NOT burn
// the (future) backend or even build the prompt. Layers are checked in order:
//
//	Eligibility — which game kinds may use llm at all (EligibleGameKinds)
//	Cadence     — per-game floor between llm attempts (MinDecisionInterval)
//	Budget      — global per-minute request cap across all games (MaxRequestsPerMinute)
//
// Eligibility lives in the snapshot but is enforced against GameContext.GameKind;
// cadence is per-game state on the Loop; the budget is one shared limiter across
// all loops (see decision/loopset.go + main.go wiring).
type LLMGate struct {
	// EligibleGameKinds is the set of GameContext.GameKind values that may use the
	// llm path. A kind not in this list gates with llm_not_eligible. Default
	// ["real"] keeps shadow games rules-only (#838).
	EligibleGameKinds []string
	// MinDecisionInterval is the per-game cadence floor: at most one LLM attempt
	// per game per interval. A cycle within the interval of the game's last
	// attempt gates with llm_interval. Default 10s.
	MinDecisionInterval time.Duration
	// MaxRequestsPerMinute is the GLOBAL request budget across all games combined.
	// Each admitted attempt is one request; an attempt that would exceed the cap
	// gates with llm_budget_exhausted. Default 6/min.
	MaxRequestsPerMinute int
	// LatencyBudget bounds a single async inference call (#917): the maximum wall
	// time an in-flight Infer may take before its result is dropped outright and the
	// cycle's deterministic rules fallback decides instead. Captured at FIRE time and
	// applied via context.WithTimeout, so a slow/hung tier never holds an apply open
	// past this. Default 8s. A non-positive value falls back to the default (never
	// "no budget", which would let a hung call leak the inference goroutine forever).
	LatencyBudget time.Duration

	// InGameAsyncUpgrade is the #1207 master switch (llm.in_game_async_upgrade). When
	// false (the default), mode="llm" is RULES-FIRST in game: every in-game cycle
	// short-circuits to the deterministic rules engine and no async LLM is fired. When
	// true, the cycle additionally evaluates the length gate below and fires the async
	// LLM upgrade only on a game long enough to absorb the latency.
	InGameAsyncUpgrade bool
	// SecondsPerPlayer is the per-player time budget for the length estimate
	// (llm.seconds_per_player): expected_seconds = ActivePlayerCount × SecondsPerPlayer
	// × StyleFactor. The async LLM upgrade fires only when expected_seconds ≥
	// LatencyBudget, so short games stay rules-only and the latency race never bites.
	SecondsPerPlayer float64
	// StyleFactor scales the expected length (llm.style_factor) — the
	// "balanced<->aggressive" knob. >1 lets the LLM weigh in on shorter games; <1
	// reserves it for longer ones. Default 1.0 (neutral).
	StyleFactor float64
}

// EligibleFor reports whether the given game kind may use the llm path. An empty
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
// restart. #927 makes them live without a restart VIA the OpenFeature provider's
// configuration-change event listener (the mechanism the maintainer requested):
// flagd emits a config-change when its source changes, the Go SDK surfaces it as
// openfeature.ProviderConfigChange, and the handler re-evaluates these four flags
// into a shared LifecycleHolder (holder.go) that the consumers read on their hot
// path. So a flag change here takes effect on the next eviction tick / decision
// cycle with NO restart and NO per-cycle polling of the flag client. All values
// are durations.
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

// ValidationConfig is the M7-7 synthetic validation gate configuration (#935):
// how many synthetic games to validate an experiment against, the fitness
// improvement bar to promote it, and whether to revert a degrading experiment.
// It mirrors the experiment.Config shape (the experiment package stays free of an
// OpenFeature dependency — the caller resolves the flags here and passes the value
// in, the same "package owns logic, caller owns flagd" split the Gate/Writer use).
type ValidationConfig struct {
	// ValidationGames is code_improvement.validation_games (default 5).
	ValidationGames int
	// ImprovementThreshold is code_improvement.fitness_improvement_threshold
	// (default 0.05): the minimum fitness improvement to promote an experiment.
	ImprovementThreshold float64
	// RevertOnDegradation is code_improvement.revert_on_degradation (default true):
	// roll back an experiment that degrades shadow fitness.
	RevertOnDegradation bool
}

// Validation evaluates the three M7-7 synthetic-validation flags (#935) for one
// validation cycle. Read live (never cached) so a stage retune takes effect on
// the next cycle. Each flag falls back to its safe default on any evaluation
// error (flagd unreachable / undefined). The numeric flags use the TYPED getters
// (the flagd numeric-flag gotcha — see llmGate); the boolean uses BooleanValue.
func (f *Flags) Validation(ctx context.Context) ValidationConfig {
	return ValidationConfig{
		ValidationGames:      f.intFlag(ctx, keyValidationGames, DefaultValidationGames),
		ImprovementThreshold: f.floatFlag(ctx, keyFitnessImprovementThreshold, DefaultFitnessImprovementThreshold),
		RevertOnDegradation:  f.boolFlag(ctx, keyRevertOnDegradation, DefaultRevertOnDegradation),
	}
}

// CodeImprovementConfig is the M7-4 propose-stage configuration (#931): which
// inference backend generates the experiment proposal, and the cadence floor
// between proposals. It mirrors experiment.ProposerConfig (the experiment package
// stays free of an OpenFeature dependency — the caller resolves the flags here
// and maps them, the same "package owns logic, caller owns flagd" split the
// Gate/Writer/Validator use).
type CodeImprovementConfig struct {
	// Engine is code_improvement.engine — the flag-selectable propose backend
	// (default "stub"). main.go uses it to select the backend; today every value
	// resolves to the inert stub until a real transport lands (#738/#742).
	Engine string
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
		Engine:      f.stringFlag(ctx, keyCodeImprovementEngine, DefaultCodeImprovementEngine),
		MinInterval: f.durationFlag(ctx, keyCodeImprovementMinInterval, DefaultCodeImprovementMinIntervalSeconds),
	}
}

// PromotionConfig is the M7-8 promotion-flow configuration (#936): the live mode
// (issue|pr|autonomous) and target (local|github) the promoter acts under. It
// mirrors the promote.Config shape (the promote package stays free of an
// OpenFeature dependency — the caller resolves the flags here, adds the kill-switch
// from the Snapshot, and passes the value in: the same "package owns logic, caller
// owns flagd" split the Gate/Writer/Validator use). The SAFE defaults are applied
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

// ExperimentDefaults carries the BOOTSTRAP defaults for the experiment runtime
// knobs (#1044): the values resolved ONCE at startup from the AGENT_EXPERIMENT_*
// env vars (#1041/#1043). Each Experiment() read falls back to the matching field
// here when its agent.json flag is absent/unreadable — so flag overrides, env is
// the floor, and a fresh agent.json (no new flags) reproduces byte-identical
// env-based behavior. The caller (experiment_loop.go) builds this from the env
// resolvers it already owns and hands it in, keeping the flags package free of any
// AGENT_EXPERIMENT_* env knowledge.
type ExperimentDefaults struct {
	// Enabled is AGENT_EXPERIMENTS_ENABLED (the loop opt-in).
	Enabled bool
	// DynamicEnabled is AGENT_EXPERIMENT_DYNAMIC_ENABLED (the #995 declarer opt-in).
	DynamicEnabled bool
	// Tick is AGENT_EXPERIMENT_TICK_SECONDS as a duration (the AllocateAndSpawn cadence).
	Tick time.Duration
	// DynamicMaxConcurrent is AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT.
	DynamicMaxConcurrent int
	// VerdictMinN is AGENT_VERDICT_MIN_N.
	VerdictMinN int
	// VerdictMinPairs is AGENT_VERDICT_MIN_PAIRS.
	VerdictMinPairs int
	// MaxGames is AGENT_EXPERIMENT_MAX_GAMES (per-experiment termination budget).
	MaxGames int
	// ShadowEffectiveConcurrency is AGENT_SHADOW_EFFECTIVE_CONCURRENCY.
	ShadowEffectiveConcurrency int
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

// ExperimentConfig is the LIVE-resolved experiment runtime configuration (#1044),
// re-read each experiment tick so a flag flip takes effect with no restart. Every
// field mirrors an ExperimentDefaults field, with the agent.json flag taking
// precedence when present and the env-derived default applying otherwise.
type ExperimentConfig struct {
	// Enabled gates the WHOLE experiment loop (experiments_enabled). When false the
	// loop does nothing — no rehydrate-spawn, no seed, no allocate. Flipping it on
	// at runtime starts the loop; flipping it off aborts every live experiment.
	Enabled bool
	// DynamicEnabled gates the #995 heuristic declarer (experiment_dynamic_enabled).
	// When false the loop declares only the static env seed (if any).
	DynamicEnabled bool
	// Tick is the AllocateAndSpawn cadence (experiment_tick_seconds). A non-positive
	// flag value falls back to the env default (durationFlag guard).
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
}

// Experiment evaluates the migrated experiment runtime knobs (#1044, #1214) for one
// experiment tick. Read LIVE (never cached) so a hot-reload of any knob takes
// effect on the next tick. Each flag falls back to the matching ExperimentDefaults
// field (the env-derived bootstrap default) on any evaluation error or flag
// absence — so flag overrides, env is the floor, and a fresh agent.json reproduces
// byte-identical env behavior. Numeric flags use the TYPED getters (the flagd
// numeric-flag gotcha — see llmGate); the duration uses durationFlag (which floors
// a non-positive value at the default).
func (f *Flags) Experiment(ctx context.Context, def ExperimentDefaults) ExperimentConfig {
	return ExperimentConfig{
		Enabled:                    f.boolFlag(ctx, keyExperimentsEnabled, def.Enabled),
		DynamicEnabled:             f.boolFlag(ctx, keyExperimentDynamicEnabled, def.DynamicEnabled),
		Tick:                       f.durationFlag(ctx, keyExperimentTickSeconds, def.Tick.Seconds()),
		DynamicMaxConcurrent:       f.intFlag(ctx, keyExperimentDynamicMaxConcur, def.DynamicMaxConcurrent),
		VerdictMinN:                f.intFlag(ctx, keyVerdictMinN, def.VerdictMinN),
		VerdictMinPairs:            f.intFlag(ctx, keyVerdictMinPairs, def.VerdictMinPairs),
		MaxGames:                   f.intFlag(ctx, keyExperimentMaxGames, def.MaxGames),
		ShadowEffectiveConcurrency: f.intFlag(ctx, keyExperimentShadowConcurrency, def.ShadowEffectiveConcurrency),
		// Verdict-tuning float knobs (#1214). Typed float getter (the flagd numeric-flag
		// gotcha — see llmGate); fail-OPEN to the env-derived default (the verdict's code
		// default) on any error/absence, exactly like the min_n/min_pairs siblings above.
		VerdictEffectThreshold: f.floatFlag(ctx, keyVerdictEffectThreshold, def.VerdictEffectThreshold),
		VerdictMinRawEffect:    f.floatFlag(ctx, keyVerdictMinRawEffect, def.VerdictMinRawEffect),
		VerdictSDFloor:         f.floatFlag(ctx, keyVerdictSDFloor, def.VerdictSDFloor),
		VerdictAnchorMargin:    f.floatFlag(ctx, keyVerdictAnchorMargin, def.VerdictAnchorMargin),
	}
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
	// LLMGate holds the three-layer LLM call gate (#847): eligibility, per-game
	// cadence, and the global per-minute request budget.
	LLMGate LLMGate
	// ContextGames is the M7-2 rolling context-window size (#929, llm.context_games):
	// how many recent game summaries to inject into this cycle's llm prompt as the
	// cross-game narrative context block. Read every cycle so retuning N is live; the
	// decision path clamps it to [0, gamewindow.RetentionCap] before assembling the
	// block. A non-positive value injects no prior games (the block renders the
	// explicit "(no prior games)" marker).
	ContextGames int
	// PromptContextNote is the M7-3 RAW operator note (#930, prompt.context_note): the
	// free-text value an operator set live to append to the system prompt. Read every
	// cycle so a runtime change is honored on the next llm call. It is the UNVALIDATED
	// raw flag value — the decision path runs llm.ValidateContextNote on it (length
	// bound + control-char/empty rejection) before injecting, so an invalid/oversized
	// note never reaches the model. Empty when unset or flagd-unreachable.
	PromptContextNote string
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
		LLMGate: f.llmGate(ctx),
		// M7-2 (#929): the cross-game context-window size, read every cycle so a
		// runtime change to llm.context_games is honored on the next llm call. Uses the
		// typed intFlag getter (the flagd numeric-flag gotcha: a numeric flag read via
		// ObjectValue resolves as a silent TYPE_MISMATCH default — see llmGate).
		ContextGames: f.intFlag(ctx, keyLLMContextGames, DefaultLLMContextGames),
		// M7-3 (#930): the raw operator note, read every cycle (live) via the string
		// getter so a runtime change to prompt.context_note is honored on the next llm
		// call. Validation/length-bounding happens on the decision side
		// (llm.ValidateContextNote), not here — this snapshot carries the raw value and
		// fails safe to "" (no note) on any flagd error.
		PromptContextNote: f.stringFlag(ctx, keyPromptContextNote, DefaultPromptContextNote),
		Fitness: Fitness{
			EnduranceMinSessionSeconds:     f.intFlag(ctx, keyEnduranceMinSessionSeconds, DefaultEnduranceMinSessionSeconds),
			BalancedMaxSkillGap:            f.floatFlag(ctx, keyBalancedMaxSkillGap, DefaultBalancedMaxSkillGap),
			BalancedSpikeSurvivalThreshold: f.floatFlag(ctx, keyBalancedSpikeSurvival, DefaultBalancedSpikeSurvivalThreshold),
			AccelerateTargetSessionSeconds: f.intFlag(ctx, keyAccelerateTargetSessionSecs, DefaultAccelerateTargetSessionSeconds),
		},
		BluetoothFitness: f.BluetoothFitness(ctx),
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
// master: when true the agent's ActionSink WRITES decisions to interventions.json;
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

// RolloutEnabled resolves the runtime rollout-actuation gate (#1213,
// rollout_enabled). It is the migrated AGENT_ROLLOUT_ENABLED env master: when true
// the infra loop's actuator FLIPS current_controller_count in rollout.json; when
// false (or flagd unreachable) it is a DRY-RUN — the loop decides+spans the
// expansion (rollout.dry_run=true) but writes nothing. Read at USE-TIME (per
// SetControllerCount / DryRun call), NOT cached at startup — flipping the flag in
// flagd starts/stops applying with no restart, and a flagd outage at any moment
// FAILS CLOSED (DefaultRolloutEnabled=false) so the agent never flips the rollout
// stage when it cannot confirm the gate is on (#1177).
func (f *Flags) RolloutEnabled(ctx context.Context) bool {
	return f.boolFlag(ctx, keyRolloutEnabled, DefaultRolloutEnabled)
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
		MinDecisionInterval:  f.durationFlag(ctx, keyLLMMinDecisionInterval, DefaultLLMMinDecisionIntervalSeconds),
		MaxRequestsPerMinute: f.intFlag(ctx, keyLLMMaxRequestsPerMin, DefaultLLMMaxRequestsPerMinute),
		// durationFlag floors a non-positive value at the default, so a misconfigured
		// budget can never disable the timeout and leak an inference goroutine (#917).
		LatencyBudget: f.durationFlag(ctx, keyLLMLatencyBudget, DefaultLLMLatencyBudgetSeconds),
		// #1207 rules-first async upgrade. The master switch reads via the bool getter;
		// the two estimate parameters read via the float getter (the flagd numeric-flag
		// gotcha above — a numeric flag read through ObjectValue silently TYPE_MISMATCHes
		// to its default). All three fail closed: switch off, defaults that keep short
		// games rules-only, so a down control plane reverts to immediate rules.
		InGameAsyncUpgrade: f.boolFlag(ctx, keyLLMInGameAsyncUpgrade, DefaultLLMInGameAsyncUpgrade),
		SecondsPerPlayer:   f.floatFlag(ctx, keyLLMSecondsPerPlayer, DefaultLLMSecondsPerPlayer),
		StyleFactor:        f.floatFlag(ctx, keyLLMStyleFactor, DefaultLLMStyleFactor),
	}
}

// eligibleGameKinds resolves the llm.eligible_game_kinds array into []string. The
// flag's variants are JSON arrays, surfaced as []any of strings (same shape as
// interventions_allowed). On any error or unparseable shape it falls back to the
// default ["real"] — fail-closed to shadow-games-rules-only, NOT to empty, which
// would disable llm entirely.
func (f *Flags) eligibleGameKinds(ctx context.Context) []string {
	raw, err := f.client.ObjectValue(ctx, keyLLMEligibleGameKinds, defaultLLMEligibleGameKinds(), openfeature.EvaluationContext{})
	if err != nil {
		f.log.Debug("flags.llm.eligible_game_kinds fell back to default", "error", err)
		return defaultLLMEligibleGameKinds()
	}
	list, ok := toStringSlice(raw)
	if !ok {
		f.log.Warn("flags.llm.eligible_game_kinds had unexpected shape, using default", "value", raw)
		return defaultLLMEligibleGameKinds()
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
