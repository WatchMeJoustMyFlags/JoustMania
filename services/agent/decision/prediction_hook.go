package decision

import (
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/prediction"
	"github.com/joustmania/agent/telemetry/keys"
)

// prediction_hook.go connects the decide records to the shared prediction book
// (#2479, UC-040 A44, BR-034). Observation only: a prediction is registered after
// the decision ran, never changes what was decided, allowed or written, and lives
// on decide.Response and decideRecord only, never on Decision or DecisionOutcome
// (the rules golden marshals those).

// SetPredictionBook injects the SHARED prediction book. main.go builds one and
// calls this on every per-game loop. nil (the default) registers nothing. Set at
// construction, before the loop serves.
func (l *Loop) SetPredictionBook(b *prediction.Book) { l.predictions = b }

// observePredictions lets the book score this game's early-decided predictions
// and register its due base-rate control (flag on only).
func (l *Loop) observePredictions(snapshot flags.Snapshot, c gamecontext.GameContext) {
	if l.predictions == nil || snapshot.Decision.PredictionsOff {
		return
	}
	l.predictions.Observe(c)
}

// remotePrediction is a remote answer's prediction: the backend's own when it
// stated one, else the lever default of the mapped decision.
func remotePrediction(resp decide.Response, d Decision, current gamecontext.GameContext) *decide.Prediction {
	if resp.Prediction != nil {
		return resp.Prediction
	}
	return decide.LeverPrediction(d.Intervention, d.Value, d.TargetSerial, current.Session.MusicTempo)
}

// predictionOutcomes are the decide outcomes whose prediction is registered: an
// acted or rules-fallback decision (acted when its write was applied, else a
// counterfactual), a held one and one discarded after mapping (counterfactuals).
var predictionOutcomes = map[string]bool{
	DecideOutcomeAct:       true,
	DecideOutcomeFallback:  true,
	DecideOutcomeHeld:      true,
	DecideOutcomeDiscarded: true,
}

// registerPrediction registers rec's prediction with the book, linked to span,
// and records the returned id on rec for the agent.decide attributes.
func (l *Loop) registerPrediction(span trace.Span, rec *decideRecord) {
	if l.predictions == nil || rec.predictionsOff || rec.prediction == nil || rec.predictionCtx == nil ||
		!predictionOutcomes[rec.outcome] {
		return
	}
	acted := (rec.outcome == DecideOutcomeAct || rec.outcome == DecideOutcomeFallback) &&
		rec.permissionKnown && !rec.blocked && rec.applied
	id, merged, ok := l.predictions.Register(prediction.Registration{
		Backend:    rec.used,
		Prediction: *rec.prediction,
		Acted:      acted,
		DecideSpan: span.SpanContext(),
		Context:    *rec.predictionCtx,
	})
	if ok {
		rec.predictionID, rec.predictionMerged = id, merged
	}
}

// predictionAttributes are the decide.prediction.* attributes of a registered
// prediction (none otherwise).
func predictionAttributes(rec *decideRecord) []attribute.KeyValue {
	if rec.predictionID == "" || rec.prediction == nil {
		return nil
	}
	p := rec.prediction
	attrs := []attribute.KeyValue{
		attribute.String(keys.AttrDecidePredictionID, rec.predictionID),
		attribute.String(keys.AttrDecidePredictionTemplate, p.Template),
		attribute.String(keys.AttrDecidePredictionCharacteristic, p.Characteristic),
		attribute.String(keys.AttrDecidePredictionScope, p.Scope),
		attribute.Int(keys.AttrDecidePredictionHorizonS, p.HorizonS),
		attribute.Float64(keys.AttrDecidePredictionP, p.P),
		attribute.String(keys.AttrDecidePredictionSource, p.Source),
	}
	if p.Rule != "" {
		attrs = append(attrs, attribute.String(keys.AttrDecidePredictionRule, p.Rule))
	}
	if rec.predictionMerged {
		attrs = append(attrs, attribute.Bool(keys.AttrDecidePredictionMerged, true))
	}
	return attrs
}
