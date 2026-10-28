package decision

import "github.com/joustmania/agent/telemetry/keys"

// Temporary aliases: the telemetry key constants moved to telemetry/keys (#2320).
// New code should import keys directly.
const (
	AttrDecisionAction            = keys.AttrDecisionAction
	AttrDecisionApplied           = keys.AttrDecisionApplied
	AttrDecisionBlocked           = keys.AttrDecisionBlocked
	AttrDecisionInterventionID    = keys.AttrDecisionInterventionID
	AttrDecisionNotAppliedReason  = keys.AttrDecisionNotAppliedReason
	AttrDecisionObjective         = keys.AttrDecisionObjective
	AttrDecisionReason            = keys.AttrDecisionReason
	AttrEffectAborted             = keys.AttrEffectAborted
	AttrEffectBaseline            = keys.AttrEffectBaseline
	AttrEffectDelta               = keys.AttrEffectDelta
	AttrEffectFollowUp            = keys.AttrEffectFollowUp
	AttrEffectIntervention        = keys.AttrEffectIntervention
	AttrEffectInterventionID      = keys.AttrEffectInterventionID
	AttrEffectObjective           = keys.AttrEffectObjective
	AttrEffectSignal              = keys.AttrEffectSignal
	AttrEffectWindowSeconds       = keys.AttrEffectWindowSeconds
	AttrEnabled                   = keys.AttrEnabled
	AttrExperimentArm             = keys.AttrExperimentArm
	AttrExperimentArms            = keys.AttrExperimentArms
	AttrExperimentFitness         = keys.AttrExperimentFitness
	AttrExperimentFlagKey         = keys.AttrExperimentFlagKey
	AttrExperimentID              = keys.AttrExperimentID
	AttrExperimentObjective       = keys.AttrExperimentObjective
	AttrExperimentStatus          = keys.AttrExperimentStatus
	AttrExperimentTargetN         = keys.AttrExperimentTargetN
	AttrExperimentThesis          = keys.AttrExperimentThesis
	AttrExperimentsEnabled        = keys.AttrExperimentsEnabled
	AttrFitnessEvaluated          = keys.AttrFitnessEvaluated
	AttrGameDurationSeconds       = keys.AttrGameDurationSeconds
	AttrGameEliminationCount      = keys.AttrGameEliminationCount
	AttrGameFinalActivePlayers    = keys.AttrGameFinalActivePlayers
	AttrGameID                    = keys.AttrGameID
	AttrGameKind                  = keys.AttrGameKind
	AttrGameTraceID               = keys.AttrGameTraceID
	AttrGameTraceSpanID           = keys.AttrGameTraceSpanID
	AttrGameWinner                = keys.AttrGameWinner
	AttrInferenceConfigured       = keys.AttrInferenceConfigured
	AttrInferenceFallback         = keys.AttrInferenceFallback
	AttrInferenceUsed             = keys.AttrInferenceUsed
	AttrInterventionsAllowed      = keys.AttrInterventionsAllowed
	AttrInterventionsEnabled      = keys.AttrInterventionsEnabled
	AttrLLMEligibleGameKinds      = keys.AttrLLMEligibleGameKinds
	AttrLLMInferError             = keys.AttrLLMInferError
	AttrLLMRetroBytes             = keys.AttrLLMRetroBytes
	AttrLLMRetroLatencyMs         = keys.AttrLLMRetroLatencyMs
	AttrLLMRetroParseOK           = keys.AttrLLMRetroParseOK
	AttrLLMRetroResponseSHA       = keys.AttrLLMRetroResponseSHA
	AttrLLMRetroSessionAssessment = keys.AttrLLMRetroSessionAssessment
	AttrLLMRetroSessionFocus      = keys.AttrLLMRetroSessionFocus
	AttrLLMRetroSuggestionCount   = keys.AttrLLMRetroSuggestionCount
	AttrLLMRetroSystemSHA         = keys.AttrLLMRetroSystemSHA
	AttrLLMRetroUser              = keys.AttrLLMRetroUser
	AttrMode                      = keys.AttrMode
	AttrObjectives                = keys.AttrObjectives
	AttrPlayerName                = keys.AttrPlayerName
	AttrPlayerSerial              = keys.AttrPlayerSerial
	AttrPolicyBatteryThreshold    = keys.AttrPolicyBatteryThreshold
	AttrPolicyMaxPerMinute        = keys.AttrPolicyMaxPerMinute
	AttrRetroSuggestionEmphasis   = keys.AttrRetroSuggestionEmphasis
	AttrRetroSuggestionReason     = keys.AttrRetroSuggestionReason
	AttrRetroSuggestionType       = keys.AttrRetroSuggestionType
	AttrSessionID                 = keys.AttrSessionID
	AttrVerdictDelta              = keys.AttrVerdictDelta
	AttrVerdictOutcome            = keys.AttrVerdictOutcome
	AttrVerdictSignificant        = keys.AttrVerdictSignificant
	SignalReceived                = keys.SignalReceived
	SpanAction                    = keys.SpanAction
	SpanDecision                  = keys.SpanDecision
	SpanDisabled                  = keys.SpanDisabled
	SpanExperiment                = keys.SpanExperiment
	SpanExperimentEvaluate        = keys.SpanExperimentEvaluate
	SpanExperimentFitness         = keys.SpanExperimentFitness
	SpanExperimentVerdict         = keys.SpanExperimentVerdict
	SpanInterventionEffect        = keys.SpanInterventionEffect
	SpanLLMRetro                  = keys.SpanLLMRetro
	SpanLLMRetroFailed            = keys.SpanLLMRetroFailed
	SpanLLMRetroResponseRef       = keys.SpanLLMRetroResponseRef
	SpanLLMRetroSuggestion        = keys.SpanLLMRetroSuggestion
)
