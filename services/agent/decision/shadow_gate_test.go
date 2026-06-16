package decision

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// agentFlagDoc is the minimal shape needed to read interventions_allowed variants
// from a flagd agent flag file. Each variant value is held as RawMessage because
// interventions_allowed migrated from a LIST flag to a STRING flag of
// comma-separated ids (#1127 — the flagd RPC list-flag trap), so a variant may be
// either a JSON array (legacy) or a comma-separated string. readInterventionsAllowed
// normalizes both into []string.
type agentFlagDoc struct {
	Flags struct {
		InterventionsAllowed struct {
			Variants map[string]json.RawMessage `json:"variants"`
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
	if len(doc.Flags.InterventionsAllowed.Variants) == 0 {
		t.Fatalf("%s: interventions_allowed has no variants", path)
	}
	out := make(map[string][]string, len(doc.Flags.InterventionsAllowed.Variants))
	for name, rawVal := range doc.Flags.InterventionsAllowed.Variants {
		out[name] = parseAllowedVariant(t, path, name, rawVal)
	}
	return out
}

// parseAllowedVariant decodes one interventions_allowed variant value, accepting
// either the STRING form ("a,b,c"; #1127) or the legacy JSON-array form.
func parseAllowedVariant(t *testing.T, path, name string, rawVal json.RawMessage) []string {
	t.Helper()
	var asString string
	if err := json.Unmarshal(rawVal, &asString); err == nil {
		ids := make([]string, 0)
		for _, tok := range strings.Split(asString, ",") {
			if s := strings.TrimSpace(tok); s != "" {
				ids = append(ids, s)
			}
		}
		return ids
	}
	var asList []string
	if err := json.Unmarshal(rawVal, &asList); err == nil {
		return asList
	}
	t.Fatalf("%s: interventions_allowed variant %q has unexpected shape: %s", path, name, rawVal)
	return nil
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

// TestPartialShieldIsShadowOnly is the load-bearing gating proof for #1129 (#1103
// Phase 2): partial_shield MUST appear ONLY in the shadow_experimental variant of
// interventions_allowed and MUST be absent from every real-facing variant
// (ambient/standard/full). The allow-list is the enforcement gate, so absence from
// the real variants means partial_shield is rejected for real games by
// construction. Asserted on BOTH the production and CI flagd agent configs.
func TestPartialShieldIsShadowOnly(t *testing.T) {
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
			if !contains(shadow, InterventionPartialShield) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionPartialShield)
			}

			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionPartialShield) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionPartialShield)
				}
			}
		})
	}
}

// TestSoftPenaltyIsShadowOnly is the load-bearing gating proof for #1134 (#1103
// Phase 3): soft_penalty MUST appear ONLY in the shadow_experimental variant of
// interventions_allowed and MUST be absent from every real-facing variant
// (ambient/standard/full). The allow-list is the enforcement gate, so absence from
// the real variants means soft_penalty (incl. the riskier tighten) is rejected for
// real games by construction. Asserted on BOTH the production and CI flagd configs.
func TestSoftPenaltyIsShadowOnly(t *testing.T) {
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
			if !contains(shadow, InterventionSoftPenalty) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionSoftPenalty)
			}

			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionSoftPenalty) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionSoftPenalty)
				}
			}
		})
	}
}
