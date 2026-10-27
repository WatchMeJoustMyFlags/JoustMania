package gamerunner

import (
	"context"
	"strings"
	"testing"

	gcpb "github.com/joustmania/agent/gen/game_coordinator"
	"github.com/joustmania/agent/patterngame"
)

func policyArchetype(t *testing.T, policy map[string]any) patterngame.Archetype {
	t.Helper()
	raw := map[string]any{
		"players": 3.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.01, "amp": 0.1,
		"period_s": 3.0, "phase": 0.0, "burst_h": 0.9, "burst_period_s": 6.0,
		"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
	}
	if policy != nil {
		raw[patterngame.TriggerPolicyKey] = policy
	}
	a, errs := patterngame.NewArchetype("unit_policy", raw)
	if len(errs) > 0 {
		t.Fatalf("archetype: %v", errs)
	}
	return a
}

// UC: UC-046 BR-018
func TestRunShadowGame_PatternTriggerPolicyStartField(t *testing.T) {
	cases := []struct {
		name   string
		policy map[string]any
		want   bool
	}{
		{"burst", map[string]any{"kind": "burst"}, true},
		{"threshold", map[string]any{"kind": "threshold", "level": 1.5, "reaction_frames": 2.0, "hold_frames": 4.0}, true},
		{"none", map[string]any{"kind": "none"}, false},
		{"absent", nil, false},
	}
	for _, c := range cases {
		t.Run("UC-046/BR-018 start field and installed archetype agree: "+c.name, func(t *testing.T) {
			a := policyArchetype(t, c.policy)
			mock := patternMock(a, map[int]uint64{0: 900, 1: 600, 2: 300}, -1)
			coord := &fakeCoord{gameID: "game_pol", events: []*gcpb.GameEvent{ev("game_starting"), ev("game_ended")}}
			h := newHarness(t, mock, coord)
			defer h.stop()
			if _, err := h.runner.RunShadowGame(context.Background(), patternSpec(a)); err != nil {
				t.Fatalf("RunShadowGame: %v", err)
			}
			mock.mu.Lock()
			params := mock.lastAdd.GetArchetypeParams()
			mock.mu.Unlock()
			got := coord.capturedStartConfig().GetPatternTriggerPolicy()
			installed := strings.Contains(params, `"trigger_policy"`)
			if got != c.want || installed != c.want {
				t.Errorf("pattern_trigger_policy=%v, installed policy=%v (%s), want both %v", got, installed, params, c.want)
			}
		})
	}
	t.Run("UC-046/BR-018 a non-pattern start never sets the field", func(t *testing.T) {
		if buildStartConfig(Spec{GameName: "JoustFFA"}, []string{"a", "b"}).GetPatternTriggerPolicy() {
			t.Error("field set without a pattern")
		}
	})
}
