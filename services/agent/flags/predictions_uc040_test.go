package flags

import (
	"context"
	"errors"
	"testing"
)

// UC: UC-040 A44 BR-034
// TestEvaluate_PredictionsEnabled: decision.predictions_enabled defaults on, and a
// missing or unreadable value reads on (#2479: it only observes).
func TestEvaluate_PredictionsEnabled(t *testing.T) {
	for _, tc := range []struct {
		name    string
		bools   map[string]bool
		errs    map[string]error
		wantOff bool
	}{
		{"UC-040/A44 BR-034 off stops predictions", map[string]bool{keyDecisionPredictionsEnabled: false}, nil, true},
		{"UC-040/A44 BR-034 on keeps them", map[string]bool{keyDecisionPredictionsEnabled: true}, nil, false},
		{"UC-040/A44 BR-034 a missing flag reads on", nil, nil, false},
		{"UC-040/A44 BR-034 a read error reads on", nil, map[string]error{keyDecisionPredictionsEnabled: errors.New("boom")}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := New(stubEvaluator{booleans: tc.bools, errs: tc.errs}, nil).Evaluate(context.Background()).Decision
			if got.PredictionsOff != tc.wantOff {
				t.Fatalf("PredictionsOff = %v, want %v", got.PredictionsOff, tc.wantOff)
			}
		})
	}
}

// UC: UC-040 A45 BR-034
// TestEvaluate_PredictionsAskModel: decision.predictions.ask_model defaults off,
// and a missing or unreadable value reads off (the request bytes stay v1).
func TestEvaluate_PredictionsAskModel(t *testing.T) {
	for _, tc := range []struct {
		name  string
		bools map[string]bool
		errs  map[string]error
		want  bool
	}{
		{"UC-040/A45 BR-034 on asks the model", map[string]bool{keyDecisionPredictionsAskModel: true}, nil, true},
		{"UC-040/A45 BR-034 off does not", map[string]bool{keyDecisionPredictionsAskModel: false}, nil, false},
		{"UC-040/A45 BR-034 a missing flag reads off", nil, nil, false},
		{"UC-040/A45 BR-034 a read error reads off", nil, map[string]error{keyDecisionPredictionsAskModel: errors.New("boom")}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := New(stubEvaluator{booleans: tc.bools, errs: tc.errs}, nil).Evaluate(context.Background()).Decision
			if got.PredictionsAskModel != tc.want {
				t.Fatalf("PredictionsAskModel = %v, want %v", got.PredictionsAskModel, tc.want)
			}
		})
	}
}
