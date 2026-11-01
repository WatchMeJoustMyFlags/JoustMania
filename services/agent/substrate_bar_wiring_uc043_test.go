package main

import (
	"context"
	"testing"
)

// substrate_bar_wiring_uc043_test.go: buildPromoter sets the substrate validity bar
// from the operator's env ceiling (UC-043 A23, FR-152 g, #2541): unset validates
// nothing, an entry counts only for the running release.

// UC: UC-043 A23
func TestUC043_A23_BuildPromoterSetsTheBarFromEnv(t *testing.T) {
	for _, tc := range []struct {
		name, env, version string
		balanced          bool
	}{
		{"unset env validates nothing", "", "0.14.0", false},
		{"an entry for the running release counts", "balanced@0.14.0", "0.14.0", true},
		{"an entry for another release does not", "balanced@0.13.0", "0.14.0", false},
		{"a moving tag never counts", "balanced@latest", "latest", false},
	} {
		t.Run("UC-043/A23 T8 "+tc.name, func(t *testing.T) {
			t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
			t.Setenv("AGENT_AUTONOMOUS_VALIDATED_OBJECTIVES", tc.env)
			t.Setenv("SERVICE_VERSION", tc.version)
			p := buildPromoter(context.Background(), nil, nil, nil, testLogger())
			if got := p.SubstrateValidated("balanced"); got != tc.balanced {
				t.Fatalf("balanced validated = %v, want %v", got, tc.balanced)
			}
			if p.SubstrateValidated("endurance") {
				t.Fatal("endurance validated without an entry")
			}
		})
	}
}
