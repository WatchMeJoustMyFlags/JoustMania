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
// decision.backend selects WHO answers the in-game decide contract. When it is
// anything other than "rules", the remote backend owns the decision and the legacy
// mode / model flags are IGNORED for in-game decisions (they keep driving the
// legacy llm path only while decision.backend=rules).
//
// Numeric flags use the TYPED getters (intFlag / floatFlag), never ObjectValue: a
// numeric flag read via ObjectValue silently TYPE_MISMATCHes to its default
// (#903/#1127).
const (
	keyDecisionBackend                = "decision.backend"
	keyDecisionActConfidenceThreshold = "decision.act_confidence_threshold"
	keyDecisionCaptureContent         = "decision.capture_content"
	keyDecisionMinIntervalMs          = "decision.min_interval_ms"
	keyDecisionClaudeModel            = "decision.claude.model"
	keyDecisionClaudeTimeoutMs        = "decision.claude.timeout_ms"
	keyDecisionOpenAIBaseURL          = "decision.openai.base_url"
	keyDecisionOpenAIModel            = "decision.openai.model"
	keyDecisionOpenAITimeoutMs        = "decision.openai.timeout_ms"
	keyDecisionOpenAIReasoningEffort  = "decision.openai.reasoning_effort"
	keyDecisionJevBaseURL             = "decision.jev.base_url"
	keyDecisionJevModel               = "decision.jev.model"
	keyDecisionJevTimeoutMs           = "decision.jev.timeout_ms"
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
	// DefaultDecisionCaptureContent is off: GenAI content capture (prompts,
	// completions, rationale as message parts) is opt-in per the GenAI semconv.
	DefaultDecisionCaptureContent = false
	// DefaultDecisionMinIntervalMs is the per-game cadence of remote decide calls
	// (decision.min_interval_ms): at most one call per game per interval. It
	// replaces llm.min_decision_interval_seconds while decision.backend != rules;
	// llm.max_requests_per_minute stays the global spend bound.
	DefaultDecisionMinIntervalMs = 3000
	// DefaultDecisionClaudeModel is the claude backend's model (variant "haiku").
	DefaultDecisionClaudeModel = "claude-haiku-4-5-20251001"
	// DefaultDecisionOpenAIBaseURL is the openai backend's endpoint.
	DefaultDecisionOpenAIBaseURL = "https://api.openai.com/v1"
	// DefaultDecisionOpenAIModel is empty: unset means the openai backend is
	// unavailable (and the decision falls back to rules).
	DefaultDecisionOpenAIModel = ""
	// DefaultDecisionOpenAIReasoningEffort lets the openai backend pick the
	// reasoning_effort from the model id (backends/openai shapeFor). Any other
	// value (none, minimal, low, ...) is sent verbatim to reasoning models.
	DefaultDecisionOpenAIReasoningEffort = "auto"
	// DefaultDecisionJevBaseURL / DefaultDecisionJevModel are empty until we have
	// a jev endpoint.
	DefaultDecisionJevBaseURL = ""
	DefaultDecisionJevModel   = ""
	// DefaultDecisionTimeoutMs bounds one remote decide call ("degrade, never
	// stall"): a call that has not answered within it falls back to rules.
	DefaultDecisionTimeoutMs = 2500
)

// RemoteBackendConfig is the flag-driven, non-secret part of one remote decision
// backend's configuration. API keys are env secrets and never flags.
type RemoteBackendConfig struct {
	// BaseURL is the endpoint (empty for claude, which has no base_url flag).
	BaseURL string
	// Model is the model id sent to the backend.
	Model string
	// Timeout bounds one decide call. Always positive.
	Timeout time.Duration
	// ReasoningEffort is the reasoning_effort sent to reasoning models
	// (decision.openai.reasoning_effort; lowercase, trimmed, never empty).
	// Only the openai backend has the flag; it is empty for claude and jev.
	ReasoningEffort string
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
	Claude      RemoteBackendConfig
	OpenAI      RemoteBackendConfig
	Jev         RemoteBackendConfig
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

// DecisionBackend resolves the decision-backend flag group. Evaluate calls it every
// cycle; it is exported so a caller can read just this group. Every flag falls back
// to its default on any evaluation error; an out-of-range threshold or a
// non-positive timeout also falls back (a bad flag edit must never stall a game or
// make every answer actionable).
func (f *Flags) DecisionBackend(ctx context.Context) DecisionBackend {
	return DecisionBackend{
		Backend:                normalizeBackendName(f.stringFlag(ctx, keyDecisionBackend, DefaultDecisionBackend)),
		ActConfidenceThreshold: f.actConfidenceThreshold(ctx),
		CaptureContent:         f.boolFlag(ctx, keyDecisionCaptureContent, DefaultDecisionCaptureContent),
		MinInterval:            f.minIntervalFlag(ctx),
		Claude: RemoteBackendConfig{
			Model:   f.stringFlag(ctx, keyDecisionClaudeModel, DefaultDecisionClaudeModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionClaudeTimeoutMs),
		},
		OpenAI: RemoteBackendConfig{
			BaseURL: f.stringFlag(ctx, keyDecisionOpenAIBaseURL, DefaultDecisionOpenAIBaseURL),
			Model:   f.stringFlag(ctx, keyDecisionOpenAIModel, DefaultDecisionOpenAIModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionOpenAITimeoutMs),
			ReasoningEffort: normalizeReasoningEffort(
				f.stringFlag(ctx, keyDecisionOpenAIReasoningEffort, DefaultDecisionOpenAIReasoningEffort)),
		},
		Jev: RemoteBackendConfig{
			BaseURL: f.stringFlag(ctx, keyDecisionJevBaseURL, DefaultDecisionJevBaseURL),
			Model:   f.stringFlag(ctx, keyDecisionJevModel, DefaultDecisionJevModel),
			Timeout: f.timeoutMsFlag(ctx, keyDecisionJevTimeoutMs),
		},
	}
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

func (f *Flags) minIntervalFlag(ctx context.Context) time.Duration {
	ms := f.intFlag(ctx, keyDecisionMinIntervalMs, DefaultDecisionMinIntervalMs)
	if ms < 0 {
		f.log.Warn("flags interval negative, using default", "key", keyDecisionMinIntervalMs, "value", ms, "default", DefaultDecisionMinIntervalMs)
		ms = DefaultDecisionMinIntervalMs
	}
	return time.Duration(ms) * time.Millisecond
}

func (f *Flags) timeoutMsFlag(ctx context.Context, key string) time.Duration {
	ms := f.intFlag(ctx, key, DefaultDecisionTimeoutMs)
	if ms <= 0 {
		f.log.Warn("flags timeout non-positive, using default", "key", key, "value", ms, "default", DefaultDecisionTimeoutMs)
		ms = DefaultDecisionTimeoutMs
	}
	return time.Duration(ms) * time.Millisecond
}
