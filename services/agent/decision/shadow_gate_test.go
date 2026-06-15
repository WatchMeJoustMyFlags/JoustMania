package decision

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

// agentFlagDoc is the minimal shape needed to read interventions_allowed variants
// from a flagd agent flag file.
type agentFlagDoc struct {
	Flags struct {
		InterventionsAllowed struct {
			Variants map[string][]string `json:"variants"`
		} `json:"interventions_allowed"`
	} `json:"flags"`
}

func readInterventionsAllowed(t *testing.T, path string) map[string][]string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var doc agentFlagDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal %s: %v", path, err)
	}
	v := doc.Flags.InterventionsAllowed.Variants
	if len(v) == 0 {
		t.Fatalf("%s: interventions_allowed has no variants", path)
	}
	return v
}

// TestSetPlayerHandicapIsShadowOnly is the load-bearing gating proof for #1107
// (#1103 MVP action 1): set_player_handicap MUST appear ONLY in the
// shadow_experimental variant of interventions_allowed and MUST be absent from
// every real-facing variant (ambient/standard/full). The allow-list is the
// enforcement gate (decode.go validates the LLM's chosen intervention against the
// live snapshot, and the rules engine reads the same flag), so absence from the
// real variants means set_player_handicap is rejected for real games by
// construction. Asserted on BOTH the production and CI flagd agent configs.
func TestSetPlayerHandicapIsShadowOnly(t *testing.T) {
	paths := []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
	}
	for _, path := range paths {
		t.Run(path, func(t *testing.T) {
			variants := readInterventionsAllowed(t, path)

			shadow, ok := variants["shadow_experimental"]
			if !ok {
				t.Fatalf("%s: missing shadow_experimental variant", path)
			}
			if !contains(shadow, InterventionSetPlayerHandicap) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionSetPlayerHandicap)
			}

			// Real-facing variants must NOT carry the shadow-only intervention.
			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionSetPlayerHandicap) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionSetPlayerHandicap)
				}
			}
		})
	}
}

// TestRampTempoIsShadowOnly is the load-bearing gating proof for #1117 (#1103 MVP
// action 2): ramp_tempo MUST appear ONLY in the shadow_experimental variant of
// interventions_allowed and MUST be absent from every real-facing variant
// (ambient/standard/full). The allow-list is the enforcement gate, so absence from
// the real variants means ramp_tempo is rejected for real games by construction.
// Asserted on BOTH the production and CI flagd agent configs.
func TestRampTempoIsShadowOnly(t *testing.T) {
	paths := []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
	}
	for _, path := range paths {
		t.Run(path, func(t *testing.T) {
			variants := readInterventionsAllowed(t, path)

			shadow, ok := variants["shadow_experimental"]
			if !ok {
				t.Fatalf("%s: missing shadow_experimental variant", path)
			}
			if !contains(shadow, InterventionRampTempo) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionRampTempo)
			}

			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionRampTempo) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionRampTempo)
				}
			}
		})
	}
}
