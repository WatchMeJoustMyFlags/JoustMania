package decision

import (
	"go.opentelemetry.io/otel/attribute"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"

	"github.com/joustmania/agent/llm"
)

// llmModeValue is the agent.mode value recorded on the prompt-capture span. The
// span only ever exists on an llm-mode cycle, so this is constant.
const llmModeValue = "llm"

// genAIOutputTypeJSON is gen_ai.output.type="json". The semconv v1.34.0 package
// (already in go.mod) ships GenAIOutputTypeText but no JSON constant, so the
// enum value is set via the key directly rather than bumping the semconv import.
// The agent's RESPONSE CONTRACT (see llm/prompt.go) requires a single JSON object.
var genAIOutputTypeJSON = semconv.GenAIOutputTypeKey.String("json")

// llmPromptAttrs is the input bundle for the single span-attribute builder. It
// bundles the inputs so the call site is a one-liner and the "every emission
// goes through one builder" invariant is easy to audit (mirrors
// infraDecisionAttrs in infra_telemetry.go): there is exactly ONE producer of
// agent.llm.prompt attributes, so the schema is complete on every emission.
type llmPromptAttrs struct {
	// prompt is the built System/User prompt pair plus its capability attribution
	// (resolved variant + model).
	prompt llm.Prompt
	// objectives is the cycle's evaluated objective weights, rendered with the
	// same summarizeObjectives convention as the decision span.
	objectives map[string]float64
	// allowed is the cycle's interventions allow-list, rendered with the same
	// allowedSummary helper as the decision span.
	allowed []string
	// gameKind is the GameContext.GameKind for this cycle (#845): "real"/"shadow"
	// or "" when unknown. Rendered schema-complete (empty string when unknown).
	gameKind string
	// gameID is the GameContext.SessionID for this cycle (= the real game_id since
	// PR A). Recorded as both session.id and the game.id alias (#845 PR C) so the
	// prompt-capture span is attributable to its game like the decision span.
	gameID string
}

// llmPromptAttributes is the SOLE builder of the agent.llm.prompt span attribute
// set (#739). Every emission path routes through it, so the schema is guaranteed
// complete and consistent. Every attribute is present on every emission:
//
//   - gen_ai.operation.name = "chat"        (semconv)
//   - gen_ai.request.model  = <model flag>  (semconv)
//   - gen_ai.output.type    = "json"        (semconv key; JSON per RESPONSE CONTRACT)
//   - agent.mode            = "llm"
//   - agent.prompt_variant  = <resolved variant from llm.Prompt.Variant>
//   - agent.objectives      = sorted "k=v" weights (summarizeObjectives)
//   - interventions.allowed = the allow-list summary (allowedSummary)
//   - llm.prompt.system_sha256 = the System-prompt FINGERPRINT (#1168), NOT the text
//   - llm.prompt.user       = the User prompt text (capped at maxUserPromptBytes)
//   - llm.prompt.bytes      = len(system)+len(user) (full size; system TEXT not emitted)
//   - inference.configured  = <model flag>
//   - inference.used        = "rules"   (the rules engine actually decided)
//   - inference.fallback_reason = "no_backend_available"  (#741 supplies the real
//     reason and used="llm" once a backend answers)
func llmPromptAttributes(in llmPromptAttrs) []attribute.KeyValue {
	system := in.prompt.System
	user := in.prompt.User
	return []attribute.KeyValue{
		// GenAI semantic conventions for the request the agent would have sent.
		semconv.GenAIOperationNameChat,
		semconv.GenAIRequestModel(in.prompt.Model),
		genAIOutputTypeJSON,
		// Agent attribution (same vocabulary as the decision span).
		attribute.String(AttrMode, llmModeValue),
		attribute.String(AttrGameKind, in.gameKind),
		// session.id + game.id alias (#845 PR C): attributable to the game.
		attribute.String(AttrSessionID, in.gameID),
		attribute.String(AttrGameID, in.gameID),
		attribute.String(AttrPromptVariant, in.prompt.Variant),
		attribute.String(AttrObjectives, summarizeObjectives(in.objectives)),
		attribute.String(AttrInterventionsAllowed, allowedSummary(in.allowed)),
		// #1168: the System prompt rides as a FINGERPRINT (resolvable via the
		// once-per-hash agent.llm.system_prompt reference log), NOT the full text —
		// that constant bulk drove the #1167 collector OOM. The User prompt (variable,
		// valuable) is KEPT, capped at maxUserPromptBytes. bytes still counts the full
		// system+user size so the at-a-glance number is unchanged.
		attribute.String(AttrLLMPromptSystemSHA, systemPromptSHA(system)),
		attribute.String(AttrLLMPromptUser, capUserPrompt(user)),
		attribute.Int(AttrLLMPromptBytes, len(system)+len(user)),
		// Inference attribution: the prompt was captured, but no backend ran, so
		// the rules engine decided. #741's resolve_backend() supplies the honest
		// used/fallback_reason once a backend answers.
		attribute.String(AttrInferenceConfigured, in.prompt.Model),
		attribute.String(AttrInferenceUsed, InferenceRules),
		attribute.String(AttrInferenceFallback, FallbackNoBackend),
	}
}
