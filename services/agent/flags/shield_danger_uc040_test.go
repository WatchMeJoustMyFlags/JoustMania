package flags

import (
	"context"
	"errors"
	"testing"
)

// UC: UC-040 A43 BR-032
// TestEvaluate_ShieldDanger: the danger-shield thresholds come from the agent set's
// policy.shield_danger_* flags; a missing flag or a read error gives the defaults
// (the rules clamp the rest, decision.shieldDangerParams).
func TestEvaluate_ShieldDanger(t *testing.T) {
	for _, tc := range []struct {
		name        string
		floats      map[string]float64
		ints        map[string]int64
		errs        map[string]error
		wantRatio   float64
		wantSamples int
	}{
		{"UC-040/A43 BR-032 the flag values", map[string]float64{keyShieldDangerRatio: 0.95}, map[string]int64{keyShieldDangerSamples: 5}, nil, 0.95, 5},
		{"UC-040/A43 BR-032 missing flags give 0.85 / 3", nil, nil, nil, 0.85, 3},
		{"UC-040/A43 BR-032 a read error gives 0.85 / 3", nil, nil, map[string]error{
			keyShieldDangerRatio: errors.New("boom"), keyShieldDangerSamples: errors.New("boom"),
		}, 0.85, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := New(stubEvaluator{floats: tc.floats, ints: tc.ints, errs: tc.errs}, nil).Evaluate(context.Background()).Policy
			if got.ShieldDangerRatio != tc.wantRatio || got.ShieldDangerSamples != tc.wantSamples {
				t.Fatalf("danger = %v / %d, want %v / %d", got.ShieldDangerRatio, got.ShieldDangerSamples, tc.wantRatio, tc.wantSamples)
			}
		})
	}
}
