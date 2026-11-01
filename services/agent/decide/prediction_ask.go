package decide

import (
	"slices"
	"strconv"
	"strings"
)

// prediction_ask.go is the model-stated prediction (#2479, UC-040 A45, BR-034;
// decide-contract.md §3, §4.1): the optional ffa.prediction question a claude or
// openai request carries in Request.PredictionAsk (opt-in by
// decision.predictions.ask_model), and the check of the model's answer to it.
// The ask is outside Request.Questions and its answer is split off before
// Validate, so a missing or bad prediction is dropped and never blocks, delays or
// changes a decision.

// QuestionFFAPrediction is the id of the prediction ask.
const QuestionFFAPrediction = "ffa.prediction"

// PredictionNone is the ask's "no prediction" option: the model expects nothing
// the closed vocabulary can measure (always the answer for noop).
const PredictionNone = "none"

// Reasons a model prediction is dropped (Response.PredictionDropped,
// decide.prediction.dropped_reason, agent_prediction_dropped_total{reason}).
const (
	// PredictionDropMissing: the ask was sent and the model did not answer it.
	PredictionDropMissing = "missing"
	// PredictionDropBadOption: the choice is not one of the offered options.
	PredictionDropBadOption = "bad_option"
	// PredictionDropBadConfidence: the confidence (the probability the prediction
	// comes true) is missing or not a finite number in [0,1].
	PredictionDropBadConfidence = "bad_confidence"
	// PredictionDropNoTarget: a target template (survives) without a target.
	PredictionDropNoTarget = "no_target"
	// PredictionDropInconsistent: the prediction contradicts the chosen
	// intervention (a prediction on noop, end_game or eliminate_player, whose
	// outcome is the action itself or nothing), or the ask was answered twice.
	PredictionDropInconsistent = "inconsistent"
)

// predictionText is the ask's question text. Static, so equal states render
// equal asks.
const predictionText = `What do you expect to happen if your answer is applied? Pick one option "<template>_<seconds>s" or "none"; "none" for noop and when nothing listed fits. Your confidence for this question is the probability that it happens within that many seconds.
survives: the targeted player is still alive.
no_elimination: no player is eliminated.
elimination: at least one player is eliminated.
activity_rises: the targeted player's movement (the field's without a target) rises.
closeness_rises: the alive players' movement levels get closer.
game_ends: the game ends.`

// PredictionOption is the ask option for template with horizon h, e.g.
// "survives_15s".
func PredictionOption(template string, h int) string {
	return template + "_" + strconv.Itoa(h) + "s"
}

// parsePredictionOption is the inverse of PredictionOption for a known template
// and one of its horizons.
func parsePredictionOption(opt string) (string, int, bool) {
	i := strings.LastIndexByte(opt, '_')
	if i <= 0 || !strings.HasSuffix(opt, "s") {
		return "", 0, false
	}
	template := opt[:i]
	h, err := strconv.Atoi(opt[i+1 : len(opt)-1])
	spec, ok := templates[template]
	if err != nil || !ok || !slices.Contains(spec.horizons, h) {
		return "", 0, false
	}
	return template, h, true
}

// PredictionQuestion builds the ffa.prediction ask for st: a choice over
// PredictionNone plus PredictionOption for every template and horizon valid in
// this state (sorted by template, horizons ascending). survives needs a target,
// so it is offered only while a player is alive; activity_rises falls back to
// the field and is always offered.
func PredictionQuestion(st State) *Question {
	opts := []string{PredictionNone}
	hasAlive := len(st.AliveSerials()) > 0
	for _, t := range Templates() {
		if t == TemplateSurvives && !hasAlive {
			continue
		}
		for _, h := range templates[t].horizons {
			opts = append(opts, PredictionOption(t, h))
		}
	}
	return &Question{ID: QuestionFFAPrediction, Kind: KindChoice, Text: predictionText, Options: opts}
}

// predictionInconsistent are the interventions a stated prediction contradicts:
// noop carries no prediction (v1), and the outcome of end_game and
// eliminate_player is the action itself (trivially true or false).
var predictionInconsistent = map[string]bool{
	IntvNoop:            true,
	IntvEndGame:         true,
	IntvEliminatePlayer: true,
}

// ValidatePrediction checks the model's answer a to the ask against the
// decision it came with (intervention and target are the validated ffa.intervention
// and ffa.target choices; target TargetNone or "" means none). It returns the
// prediction (Source model, Rule the intervention, P the answer's confidence), or
// nil with "" for an answer of PredictionNone, or nil with a PredictionDrop*
// reason. It never panics and never looks at a.Probabilities.
func ValidatePrediction(ask Question, a Answer, intervention, target string) (*Prediction, string) {
	if !slices.Contains(ask.Options, a.Choice) {
		return nil, PredictionDropBadOption
	}
	if !finite(a.Confidence) || a.Confidence < 0 || a.Confidence > 1 {
		return nil, PredictionDropBadConfidence
	}
	if a.Choice == PredictionNone {
		return nil, ""
	}
	template, h, ok := parsePredictionOption(a.Choice)
	if !ok {
		return nil, PredictionDropBadOption
	}
	if predictionInconsistent[intervention] {
		return nil, PredictionDropInconsistent
	}
	if target == TargetNone {
		target = ""
	}
	if template == TemplateSurvives && target == "" {
		return nil, PredictionDropNoTarget
	}
	p := NewPrediction(template, h, a.Confidence, SourceModel, intervention, target)
	if !ValidPrediction(p) {
		return nil, PredictionDropInconsistent
	}
	return &p, ""
}
