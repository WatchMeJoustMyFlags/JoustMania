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

// TestToughnessBoostIsShadowOnly is the load-bearing gating proof for #1129 (#1103
// Phase 2): toughness_boost MUST appear ONLY in the shadow_experimental variant of
// interventions_allowed and MUST be absent from every real-facing variant
// (ambient/standard/full). The allow-list is the enforcement gate, so absence from
// the real variants means toughness_boost is rejected for real games by
// construction. Asserted on BOTH the production and CI flagd agent configs.
func TestToughnessBoostIsShadowOnly(t *testing.T) {
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
			if !contains(shadow, InterventionToughnessBoost) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionToughnessBoost)
			}

			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionToughnessBoost) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionToughnessBoost)
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

// TestAutoRubberbandIsShadowOnly is the load-bearing gating proof for #1143 (#1103
// Phase 3): auto_rubberband MUST appear ONLY in the shadow_experimental variant of
// interventions_allowed and MUST be absent from every real-facing variant
// (ambient/standard/full). The allow-list is the enforcement gate, so absence from
// the real variants means auto_rubberband (a real game-affecting, gap-compressing
// intervention) is rejected for real games by construction. Asserted on BOTH the
// production and CI flagd agent configs.
func TestAutoRubberbandIsShadowOnly(t *testing.T) {
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
			if !contains(shadow, InterventionAutoRubberband) {
				t.Fatalf("%s: shadow_experimental must include %q", path, InterventionAutoRubberband)
			}

			for _, realVariant := range []string{"ambient", "standard", "full"} {
				list, ok := variants[realVariant]
				if !ok {
					t.Fatalf("%s: missing expected real variant %q", path, realVariant)
				}
				if contains(list, InterventionAutoRubberband) {
					t.Fatalf("%s: real variant %q must NOT include %q (shadow-only)", path, realVariant, InterventionAutoRubberband)
				}
			}
		})
	}
}

// TestAllowListVariantsOnlyNameKnownInterventions pins the rename contract of
// #1838: every id in every variant of interventions_allowed (production and CI
// flag files) is a known Intervention* constant. A renamed or retired id left
// behind in a flag file would otherwise become a live option with no writer
// case, no description and no game-side implementation (there is deliberately
// no alias table).
// UC: UC-040 main step 11
func TestAllowListVariantsOnlyNameKnownInterventions(t *testing.T) {
	known := map[string]bool{}
	for _, id := range []string{
		InterventionPlayAudioCue, InterventionSendControllerEffect, InterventionAdjustVolume,
		InterventionAdjustMusicTempo, InterventionAdjustPlayerSensitivity, InterventionSetPlayerHandicap,
		InterventionGrantShield, InterventionToughnessBoost, InterventionSoftPenalty,
		InterventionAdjustGlobalSensitivity, InterventionAdjustGlobalDifficulty, InterventionSetPacingProfile,
		InterventionRampTempo, InterventionAutoRubberband, InterventionEliminatePlayer,
		InterventionRevivePlayer, InterventionEndGame, InterventionGrantGroupShield, InterventionNoop,
	} {
		known[id] = true
	}
	for _, path := range []string{
		filepath.Join("..", "..", "flagd", "agent.json"),
		filepath.Join("..", "..", "flagd", "ci", "agent.json"),
	} {
		t.Run(path, func(t *testing.T) {
			for variant, ids := range readInterventionsAllowed(t, path) {
				for _, id := range ids {
					if !known[id] {
						t.Errorf("%s: variant %q lists %q, which is not a known Intervention* constant", path, variant, id)
					}
				}
			}
		})
	}
}
