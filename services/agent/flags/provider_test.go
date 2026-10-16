package flags

import (
	"context"
	"reflect"
	"testing"

	"github.com/open-feature/go-sdk/openfeature"
	"github.com/open-feature/go-sdk/openfeature/memprovider"
)

// TestEvaluate_WithInMemoryProvider exercises the wrapper end-to-end through a
// real OpenFeature client backed by the SDK's in-memory provider, mirroring the
// variant shapes in services/flagd/agent.json.
func TestEvaluate_WithInMemoryProvider(t *testing.T) {
	const domain = "agent-test"

	provider := memprovider.NewInMemoryProvider(map[string]memprovider.InMemoryFlag{
		keyEnabled: {
			State:          memprovider.Enabled,
			DefaultVariant: "on",
			Variants:       map[string]any{"on": true, "off": false},
		},
		keyObjectives: {
			State:          memprovider.Enabled,
			DefaultVariant: "endurance_focused",
			Variants: map[string]any{
				"endurance_focused": map[string]any{"endurance": 0.7, "balanced": 0.3},
			},
		},
		keyInterventionsAllowed: {
			State:          memprovider.Enabled,
			DefaultVariant: "ambient",
			// STRING flag: comma-separated ids (#1127), read via StringValue.
			Variants: map[string]any{
				"ambient": "play_audio_cue,adjust_volume",
			},
		},
		keyBatteryThreshold: {
			State:          memprovider.Enabled,
			DefaultVariant: "strict",
			Variants:       map[string]any{"strict": 30, "default": 20},
		},
		keyMovementVarianceWindow: {
			State:          memprovider.Enabled,
			DefaultVariant: "long",
			Variants:       map[string]any{"long": 20, "default": 10},
		},
		keyMaxInterventionsPerMinute: {
			State:          memprovider.Enabled,
			DefaultVariant: "aggressive",
			Variants:       map[string]any{"aggressive": 4, "default": 2},
		},
	})

	if err := openfeature.SetNamedProviderAndWait(domain, provider); err != nil {
		t.Fatalf("SetNamedProviderAndWait: %v", err)
	}
	client := openfeature.GetApiInstance().GetNamedClient(domain)

	f := New(client, nil)
	got := f.Evaluate(context.Background())

	if !got.Enabled {
		t.Errorf("Enabled = false, want true")
	}
	if !reflect.DeepEqual(got.Objectives, map[string]float64{"endurance": 0.7, "balanced": 0.3}) {
		t.Errorf("Objectives = %v", got.Objectives)
	}
	if !reflect.DeepEqual(got.InterventionsAllowed, []string{"play_audio_cue", "adjust_volume"}) {
		t.Errorf("InterventionsAllowed = %v", got.InterventionsAllowed)
	}
	wantPolicy := Policy{BatteryThreshold: 30, MovementVarianceWindow: 20, MaxInterventionsPerMinute: 4}
	if got.Policy != wantPolicy {
		t.Errorf("Policy = %+v, want %+v", got.Policy, wantPolicy)
	}
}
