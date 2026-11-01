package flags

import (
	"context"
	"math"
	"strings"
	"time"
)

// Decision-backend flags (epic #1225, PR B #1226). Flat keys in flagSetId "agent"
// (services/flagd/agent.json + ci/agent.json). Read on EVERY decision cycle as part
// of Evaluate (never cached at startup: flagd may be down when the agent starts),
// so switching the backend, its model or its timeout is one flag edit with no
// restart.
//
// decision.backend selects WHO answers the in-game decide contract: the rules
// engine (the default and floor) or a remote backend, which then owns the
// decision. It is the only in-game LLM route since #1234 retired the legacy
// mode / model flags.
//
// Numeric flags use the TYPED getters (intFlag / floatFlag), never ObjectValue: a
// numeric flag read via ObjectValue silently TYPE_MISMATCHes to its default
// (#903/#1127).
const (
	keyDecisionBackend                   = "decision.backend"
	keyDecisionActConfidenceThreshold    = "decision.act_confidence_threshold"
	keyDecisionCaptureContent            = "decision.capture_content"
	keyDecisionMinIntervalMs             = "decision.min_interval_ms"
	keyDecisionMinGapAfterAnswerMs       = "decision.min_gap_after_answer_ms"
	keyDecisionMaxStateAgeMs             = "decision.max_state_age_ms"
	keyDecisionPredictionsEnabled        = "decision.predictions_enabled"
	keyDecisionPredictionsAskModel       = "decision.predictions.ask_model"
	keyDecisionGoalInPrompt              = "decision.goal_in_prompt"
	keyDecisionClaudeModel               = "decision.claude.model"
	keyDecisionClaudeTimeoutMs           = "decision.claude.timeout_ms"
	keyDecisionClaudeBaseURL             = "decision.claude.base_url"
	keyDecisionOpenAIBaseURL             = "decision.openai.base_url"
	keyDecisionOpenAIModel               = "decision.openai.model"
	keyDecisionOpenAITimeoutMs           = "decision.openai.timeout_ms"
	keyDecisionOpenAIReasoningEffort     = "decision.openai.reasoning_effort"
	keyDecisionJevBaseURL                = "decision.jev.base_url"
	keyDecisionJevModel                  = "decision.jev.model"
	keyDecisionJevTimeoutMs              = "decision.jev.timeout_ms"
	keyDecisionJevActConfidenceThreshold = "decision.jev.act_confidence_threshold"
)

// Decision-backend names (the decision.backend variants).
const (
	DecisionBackendRules  = "rules"
	DecisionBackendClaude = "claude"
	DecisionBackendOpenAI = "openai"
	DecisionBackendJev    = "jev"
)

// Decision-backend defaults, mirroring the services/flagd/agent.json
// defaultVariants. Applied when flagd is unreachable or a flag is undefined.
const (
	// DefaultDecisionBackend is the deterministic rules engine: the floor every
	// other backend falls back to. A down control plane never calls a remote model.
	DefaultDecisionBackend = DecisionBackendRules
	// DefaultDecisionActConfidenceThreshold is the confidence at or above which a
	// remote answer is acted on; below it the answer is held (no action).
	DefaultDecisionActConfidenceThreshold = 0.8
	// DefaultDecisionJevActConfidenceThreshold is the jev backend's own act
	// threshold (decision.jev.act_confidence_threshold, variant "gate", #1287).
	// Jev's confidence is the calibrated gate p_yes (calm ~0.16, dragging ~0.35,
	// collapsing ~0.68, near-threshold ~0.72), not a self-report, so it acts at
	// 0.6 out of the box while claude/openai keep the global 0.8.
	DefaultDecisionJevActConfidenceThreshold = 0.6
	// ActConfidenceThresholdInherit is the "inherit" variant of a per-backend act
	// threshold: the backend uses decision.act_confidence_threshold.
	ActConfidenceThresholdInherit = -1.0
	// DefaultDecisionCaptureContent is off: GenAI content capture (prompts,
	// completions, rationale as message parts) is opt-in per the GenAI semconv.
	DefaultDecisionCaptureContent = false
	// DefaultDecisionMinIntervalMs is the per-game cadence of remote decide calls
	// (decision.min_interval_ms): at most one call per game per interval;
	// llm.max_requests_per_minute stays the global spend bound.
	//
	// The interval runs from when a call FIRES, and the per-game inflight guard
	// never overlaps two calls, so the effective cadence is max(interval, call
	// latency) rounded up to the next evaluate cycle. With remote calls taking
	// ~2-3.5s (#1231) that is one call every ~3-4s, i.e. ~15-20 calls a minute
	// for ONE game, a lower bound on demand since post-game retros draw on the
	// same global budget: far above the default llm.max_requests_per_minute of 6,
	// which is spent in the first ~20s of a game (then llm_budget_exhausted ->
	// rules). Raise the budget (its "stage" variant, 30) for a demo, or lengthen
	// this interval to spend less; 3000 stays the default because a shorter
	// interval would only be absorbed by the inflight guard. Note that
	// AGENT_LLM_MAX_REQUESTS_PER_MINUTE, when set, is a ceiling on the budget flag
	// (Loop.budgetCap = min(env, flag), #1788), so "stage" is capped by it.
	DefaultDecisionMinIntervalMs = 3000
	// DefaultDecisionMinGapAfterAnswerMs is decision.min_gap_after_answer_ms
	// (#2123): the next remote decide call of a game starts no earlier than this
	// long after the previous call ENDED (an answer, a timeout or a failure). It
	// is a second, separate clock next to decision.min_interval_ms (which runs
	// from when a call fires): a call is admitted only when both have elapsed.
	// Zero disables the gap.
	DefaultDecisionMinGapAfterAnswerMs = 1000
	// DefaultDecisionMaxStateAgeMs is decision.max_state_age_ms (#2123): a remote
	// answer that would act is discarded (decide.discarded_reason=state_too_old)
	// when the game state it was built on (the fire-time GameContext.UpdatedAt) is
	// older than this when the answer lands. 10 s sits above the longest call
	// timeout (claude "patient", 8 s) plus the coordinator's metric export
	// interval, so it only drops answers built on a state that was already stale
	// when the call fired. Zero disables the check.
	DefaultDecisionMaxStateAgeMs = 10000
	// DefaultDecisionClaudeModel is the claude backend's model (variant "haiku").
	DefaultDecisionClaudeModel = "claude-haiku-4-5-20251001"
	// DefaultDecisionClaudeBaseURL is the claude backend's endpoint root (#1900).
	// It has NO /v1: the Anthropic SDK appends /v1/messages itself, so a /v1 here
	// would post to /v1/v1/messages (404, every call falls back to rules).
	// Equals claude.DefaultBaseURL.
	DefaultDecisionClaudeBaseURL = "https://api.anthropic.com"
	// DefaultDecisionOpenAIBaseURL is the openai backend's endpoint.
	DefaultDecisionOpenAIBaseURL = "https://api.openai.com/v1"
	// DefaultDecisionOpenAIModel is empty: unset means the openai backend is
	// unavailable (and the decision falls back to rules).
	DefaultDecisionOpenAIModel = ""
	// DefaultDecisionOpenAIReasoningEffort lets the openai backend pick the
	// reasoning_effort from the model id (backends/openai shapeFor); "omit"
	// never sends the field; any other value (none, minimal, low, ...) is sent
	// verbatim to reasoning models.
	DefaultDecisionOpenAIReasoningEffort = "auto"
	// DefaultDecisionJevBaseURL is TypeSafe's Jev API root (#1229); the jev
	// backend posts to {base_url}/systemone.
	DefaultDecisionJevBaseURL = "https://api.typesafe.ai/v1"
	// DefaultDecisionJevModel is TypeSafe's alias for the latest stable Jev
	// (variant "latest"); variant "pinned" is a fixed version (jev-1.13.0) for
	// thresholds tuned against one release.
	DefaultDecisionJevModel = "jev-latest"
	// DefaultDecisionTimeoutMs bounds one remote decide call ("degrade, never
	// stall"): a call that has not answered within it falls back to rules. It is
	// the default of decision.openai.timeout_ms and decision.jev.timeout_ms.
	DefaultDecisionTimeoutMs = 2500
	// DefaultDecisionClaudeTimeoutMs is the decision.claude.timeout_ms default
	// (variant "default"). Measured live (#1231): Haiku 4.5 and Sonnet 5.5 answer
	// the FFA questions in ~2.3-3.4s on a warm connection and ~3.5-5.5s on the
	// first call of a cold one, so the old 2.5s timed out nearly every call. 5s
	// covers the warm p95 with ~1.5s headroom; "patient" (8s) also covers cold
	// calls, "tight" is the old 2.5s.
	DefaultDecisionClaudeTimeoutMs = 5000
)

// RemoteBackendConfig is the flag-driven, non-secret part of one remote decision
// backend's configuration. API keys are env secrets and never flags.
type RemoteBackendConfig struct {
	// BaseURL is the endpoint (decision.<backend>.base_url).
	BaseURL string
	// Model is the model id sent to the backend.
	Model string
	// Timeout bounds one decide call. Always positive.
	Timeout time.Duration
	// ReasoningEffort is the reasoning_effort sent to reasoning models
	// (decision.openai.reasoning_effort; lowercase, trimmed, never empty).
	// Only the openai backend has the flag; it is empty for claude and jev.
	ReasoningEffort string
	// ActConfidenceThreshold is the backend's own act threshold
	// (decision.<backend>.act_confidence_threshold, in [0,1]); it applies only
	// when HasActConfidenceThreshold is true, otherwise the backend inherits
	// decision.act_confidence_threshold. Only jev has the flag (#1287); the zero
	// value (claude, openai, hand-built test snapshots) inherits.
	ActConfidenceThreshold float64
	// HasActConfidenceThreshold reports whether ActConfidenceThreshold applies:
	// true when the backend's own flag resolved to a value in [0,1], false for
	// the "inherit" sentinel, an out-of-range value or a backend without the flag
	// (then DecisionBackend.ActThreshold returns the global threshold).
	HasActConfidenceThreshold bool
}

// DecisionBackend is the decision-backend flag group (#1226).
type DecisionBackend struct {
	// Backend is the normalized decision.backend value (lowercase, trimmed;
	// empty falls back to rules). Unknown names are kept verbatim so the decide
	// span can record what was requested; the registry maps them to rules.
	Backend string
	// ActConfidenceThreshold is decision.act_confidence_threshold, in [0,1].
	ActConfidenceThreshold float64
	// CaptureContent is decision.capture_content (GenAI content capture opt-in).
	CaptureContent bool
	// MinInterval is decision.min_interval_ms: the per-game cadence of remote
	// decide calls. Zero disables the cadence floor; a negative value falls back
	// to the default.
	MinInterval time.Duration
	// MinGapAfterAnswer is decision.min_gap_after_answer_ms (#2123): the per-game
	// gap between the end of one remote decide call (answer, timeout or failure)
	// and the start of the next. Zero disables the gap; a negative value falls
	// back to the default.
	MinGapAfterAnswer time.Duration
	// MaxStateAge is decision.max_state_age_ms (#2123): the oldest the state a
	// remote answer was built on may be when the answer lands and would act.
	// Zero disables the check; a negative value falls back to the default.
	MaxStateAge time.Duration
	// PredictionsOff is decision.predictions_enabled = off (#2479, UC-040 A44,
	// BR-034): the agent registers, times and scores no decision prediction and no
	// base-rate control. Default on; a missing or unreadable value reads on (it
	// only observes and cannot widen anything). Inverted so the zero value of a
	// hand-built snapshot matches the default.
	PredictionsOff bool
	// GoalInPrompt is decision.goal_in_prompt (#2479, decide-contract.md §14.4): a
	// claude or openai request carries the "goal:" / "now:" lines. The only switch:
	// decision.prompt_profile never implies them. Default off; unreadable reads off.
	GoalInPrompt bool
	// PredictionsAskModel is decision.predictions.ask_model (#2479, UC-040 A45,
	// BR-034): a claude or openai request carries the ffa.prediction ask. Default
	// off; a missing or unreadable value reads off, so the request bytes stay v1.
	PredictionsAskModel bool
	Claude              RemoteBackendConfig
	OpenAI              RemoteBackendConfig
	Jev                 RemoteBackendConfig
}

// IsRules reports whether the rules engine is the selected backend. A zero-value
// DecisionBackend (hand-built snapshots in tests) counts as rules.
func (d DecisionBackend) IsRules() bool {
	return d.Backend == "" || d.Backend == DecisionBackendRules
}

// Remote returns the flag config of the named remote backend, and false for rules
// or an unknown name.
func (d DecisionBackend) Remote(name string) (RemoteBackendConfig, bool) {
	switch name {
	case DecisionBackendClaude:
		return d.Claude, true
	case DecisionBackendOpenAI:
		return d.OpenAI, true
	case DecisionBackendJev:
		return d.Jev, true
	}
	return RemoteBackendConfig{}, false
}

// Act-threshold sources (decide.act_confidence_threshold.source): the global
// decision.act_confidence_threshold, or the answering backend's own flag (the
// backend name, e.g. "jev").
const ActThresholdSourceGlobal = "global"

// ActThreshold returns the act-confidence threshold that applies to an answer from
// the named backend, and where it came from: the backend's own
// decision.<backend>.act_confidence_threshold when it has one (source = the
// backend name), else the global decision.act_confidence_threshold (source
// "global"). Rules and unknown names always get the global.
func (d DecisionBackend) ActThreshold(backend string) (float64, string) {
	if cfg, ok := d.Remote(backend); ok && cfg.HasActConfidenceThreshold {
		return cfg.ActConfidenceThreshold, backend
	}
	return d.ActConfidenceThreshold, ActThresholdSourceGlobal
}

// DecisionBackend resolves the decision-backend flag group. Evaluate calls it every
// cycle; it is exported so a caller can read just this group. Every flag falls back
// to its default on any evaluation error; an out-of-range threshold or a
// non-positive timeout also falls back (a bad flag edit must never stall a game or
// make every answer actionable).
func (f *Flags) DecisionBackend(ctx context.Context) DecisionBackend {
	d := DecisionBackend{
		Backend:                normalizeBackendName(f.stringFlag(ctx, keyDecisionBackend, DefaultDecisionBackend)),
		ActConfidenceThreshold: f.actConfidenceThreshold(ctx),
		CaptureContent:         f.boolFlag(ctx, keyDecisionCaptureContent, DefaultDecisionCaptureContent),
		MinInterval:            f.minIntervalFlag(ctx),
		MinGapAfterAnswer:      f.nonNegativeMsFlag(ctx, keyDecisionMinGapAfterAnswerMs, DefaultDecisionMinGapAfterAnswerMs),
		MaxStateAge:            f.nonNegativeMsFlag(ctx, keyDecisionMaxStateAgeMs, DefaultDecisionMaxStateAgeMs),
		PredictionsOff:         !f.boolFlag(ctx, keyDecisionPredictionsEnabled, true),
		PredictionsAskModel:    f.boolFlag(ctx, keyDecisionPredictionsAskModel, false),
		GoalInPrompt:           f.boolFlag(ctx, keyDecisionGoalInPrompt, false),
		Claude: RemoteBackendConfig{
			BaseURL: f.stringFlag(ctx, keyDecisionClaudeBaseURL, DefaultDecisionClaudeBaseURL),
			Model:   f.stringFlag(ctx, keyDecisionClaudeModel, DefaultDecisionClaudeModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionClaudeTimeoutMs, DefaultDecisionClaudeTimeoutMs),
		},
		OpenAI: RemoteBackendConfig{
			BaseURL: f.stringFlag(ctx, keyDecisionOpenAIBaseURL, DefaultDecisionOpenAIBaseURL),
			Model:   f.stringFlag(ctx, keyDecisionOpenAIModel, DefaultDecisionOpenAIModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionOpenAITimeoutMs, DefaultDecisionTimeoutMs),
			ReasoningEffort: normalizeReasoningEffort(
				f.stringFlag(ctx, keyDecisionOpenAIReasoningEffort, DefaultDecisionOpenAIReasoningEffort)),
		},
		Jev: RemoteBackendConfig{
			BaseURL: f.stringFlag(ctx, keyDecisionJevBaseURL, DefaultDecisionJevBaseURL),
			Model:   f.stringFlag(ctx, keyDecisionJevModel, DefaultDecisionJevModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionJevTimeoutMs, DefaultDecisionTimeoutMs),
		},
	}
	d.Jev.ActConfidenceThreshold, d.Jev.HasActConfidenceThreshold = f.backendActConfidenceThreshold(
		ctx, keyDecisionJevActConfidenceThreshold, DefaultDecisionJevActConfidenceThreshold)
	return d
}

func normalizeBackendName(v string) string {
	v = strings.ToLower(strings.TrimSpace(v))
	if v == "" {
		return DefaultDecisionBackend
	}
	return v
}

// normalizeReasoningEffort lowercases and trims; empty means auto.
func normalizeReasoningEffort(v string) string {
	v = strings.ToLower(strings.TrimSpace(v))
	if v == "" {
		return DefaultDecisionOpenAIReasoningEffort
	}
	return v
}

func (f *Flags) actConfidenceThreshold(ctx context.Context) float64 {
	v := f.floatFlag(ctx, keyDecisionActConfidenceThreshold, DefaultDecisionActConfidenceThreshold)
	if math.IsNaN(v) || v < 0 || v > 1 {
		f.log.Warn("flags threshold outside [0,1], using default",
			"key", keyDecisionActConfidenceThreshold, "value", v, "default", DefaultDecisionActConfidenceThreshold)
		return DefaultDecisionActConfidenceThreshold
	}
	return v
}

// backendActConfidenceThreshold reads a per-backend act threshold. The "inherit"
// sentinel (ActConfidenceThresholdInherit) returns has=false: the backend uses the
// global flag. NaN or a value outside [0,1] also inherits the global (logged): a
// bad edit must never make every answer of one backend actionable. An evaluation
// error (flagd down, flag undefined) yields def, mirroring the defaultVariant.
func (f *Flags) backendActConfidenceThreshold(ctx context.Context, key string, def float64) (float64, bool) {
	v := f.floatFlag(ctx, key, def)
	if v == ActConfidenceThresholdInherit {
		return 0, false
	}
	if math.IsNaN(v) || v < 0 || v > 1 {
		f.log.Warn("flags threshold outside [0,1], inheriting decision.act_confidence_threshold",
			"key", key, "value", v)
		return 0, false
	}
	return v, true
}

func (f *Flags) minIntervalFlag(ctx context.Context) time.Duration {
	return f.nonNegativeMsFlag(ctx, keyDecisionMinIntervalMs, DefaultDecisionMinIntervalMs)
}

// nonNegativeMsFlag reads a millisecond flag where zero disables the bound and a
// negative value (a bad edit) falls back to def.
func (f *Flags) nonNegativeMsFlag(ctx context.Context, key string, def int) time.Duration {
	ms := f.intFlag(ctx, key, def)
	if ms < 0 {
		f.log.Warn("flags interval negative, using default", "key", key, "value", ms, "default", def)
		ms = def
	}
	return time.Duration(ms) * time.Millisecond
}

func (f *Flags) timeoutMsFlag(ctx context.Context, key string, def int) time.Duration {
	ms := f.intFlag(ctx, key, def)
	if ms <= 0 {
		f.log.Warn("flags timeout non-positive, using default", "key", key, "value", ms, "default", def)
		ms = def
	}
	return time.Duration(ms) * time.Millisecond
}
