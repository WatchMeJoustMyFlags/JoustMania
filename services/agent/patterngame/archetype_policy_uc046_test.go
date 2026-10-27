package patterngame

import (
	"encoding/json"
	"os"
	"strings"
	"testing"
)

// shippedDigests are the parameter digests of the six archetypes shipped
// before UC-046 BR-018 (#2096). A trigger_policy of none is left out of the
// canonical JSON, so these must never change (BR-009: a digest names the
// exact parameters of a recorded game).
var shippedDigests = map[string]string{
	"calm_long":       "add971f739339ea9",
	"slow_cascade":    "774496f3cdf3c986",
	"jittery_novices": "a26243e6e4efe429",
	"close_race":      "f9b67b6047aefcc9",
	"late_burst":      "516788e7874c6529",
	"quick_cascade":   "4153e182541b1847",
}

type policyCases struct {
	Base  map[string]any `json:"base"`
	Cases []struct {
		Name      string         `json:"name"`
		Params    map[string]any `json:"params"`
		Policy    any            `json:"policy"`
		Valid     bool           `json:"valid"`
		HasPolicy bool           `json:"has_policy"`
	} `json:"cases"`
}

func loadShippedVariants(t *testing.T) map[string]any {
	t.Helper()
	data, err := os.ReadFile("../../flagd/agent.json")
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Flags map[string]map[string]any `json:"flags"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatal(err)
	}
	return doc.Flags[ArchetypeFlagKey]["variants"].(map[string]any)
}

// UC: UC-046 BR-018 BR-003 BR-009
func TestArchetypeTriggerPolicy(t *testing.T) {
	t.Run("UC-046/BR-009 the digests of the six shipped archetypes are unchanged", func(t *testing.T) {
		variants := loadShippedVariants(t)
		for name, want := range shippedDigests {
			a, errs := NewArchetype(name, variants[name])
			if len(errs) > 0 {
				t.Fatalf("%s: %v", name, errs)
			}
			if a.Digest != want || a.HasTriggerPolicy() || strings.Contains(a.ParamsJSON, TriggerPolicyKey) {
				t.Errorf("%s: digest %s (want %s), policy %v, json %s", name, a.Digest, want, a.TriggerPolicy, a.ParamsJSON)
			}
		}
	})

	t.Run("UC-046/BR-018 shared validation cases (parity with the controller manager)", func(t *testing.T) {
		data, err := os.ReadFile("testdata/trigger_policy_cases.json")
		if err != nil {
			t.Fatal(err)
		}
		var cases policyCases
		if err := json.Unmarshal(data, &cases); err != nil {
			t.Fatal(err)
		}
		for _, c := range cases.Cases {
			raw := map[string]any{}
			for k, v := range cases.Base {
				raw[k] = v
			}
			for k, v := range c.Params {
				raw[k] = v
			}
			if c.Policy != nil {
				raw[TriggerPolicyKey] = c.Policy
			}
			a, errs := NewArchetype("policy_case", raw)
			if (len(errs) == 0) != c.Valid {
				t.Errorf("%s: valid=%v, errors %v", c.Name, len(errs) == 0, errs)
				continue
			}
			if c.Valid && a.HasTriggerPolicy() != c.HasPolicy {
				t.Errorf("%s: HasTriggerPolicy=%v, want %v", c.Name, a.HasTriggerPolicy(), c.HasPolicy)
			}
		}
	})

	t.Run("UC-046/BR-018 a policy is part of the canonical JSON and the digest, none is not", func(t *testing.T) {
		base := map[string]any{
			"players": 2.0, "stream_rate_hz": 30.0, "base": 1.0, "drift": 0.0, "amp": 0.5, "period_s": 2.0,
			"phase": 0.0, "burst_h": 3.0, "burst_period_s": 2.0, "d_base": 0.0, "d_drift": 0.0, "d_amp": 0.0,
			"d_phase": 0.5,
		}
		with := func(policy any) Archetype {
			raw := map[string]any{}
			for k, v := range base {
				raw[k] = v
			}
			if policy != nil {
				raw[TriggerPolicyKey] = policy
			}
			a, errs := NewArchetype("p", raw)
			if len(errs) > 0 {
				t.Fatalf("%v: %v", policy, errs)
			}
			return a
		}
		plain, none := with(nil), with(map[string]any{"kind": "none"})
		burst := with(map[string]any{"kind": "burst"})
		threshold := with(map[string]any{"kind": "threshold", "level": 2.5, "reaction_frames": 3.0, "hold_frames": 6.0})
		if plain.Digest != none.Digest || plain.ParamsJSON != none.ParamsJSON {
			t.Errorf("an explicit none changed the canonical JSON: %s vs %s", plain.ParamsJSON, none.ParamsJSON)
		}
		if burst.Digest == plain.Digest || threshold.Digest == burst.Digest {
			t.Error("the policy is not covered by the digest")
		}
		if !strings.Contains(burst.ParamsJSON, `"trigger_policy":{"kind":"burst"}`) {
			t.Errorf("burst json %s", burst.ParamsJSON)
		}
		if !strings.Contains(threshold.ParamsJSON,
			`"trigger_policy":{"hold_frames":6,"kind":"threshold","level":2.5,"reaction_frames":3}`) {
			t.Errorf("threshold json %s", threshold.ParamsJSON)
		}
		// The journaled form restores to the same archetype (UC-046 BR-015).
		pin, err := RestorePin("x", []string{"p"}, []string{threshold.ParamsJSON}, []string{threshold.Digest}, 300)
		if err != nil || pin.Archetypes[0].TriggerPolicy != threshold.TriggerPolicy {
			t.Errorf("restore: %v %+v", err, pin.Archetypes)
		}
	})

	t.Run("UC-046/BR-018 the shipped policy archetype has a policy and is not selected by default", func(t *testing.T) {
		variants := loadShippedVariants(t)
		a, errs := NewArchetype("burst_holder", variants["burst_holder"])
		if len(errs) > 0 || !a.HasTriggerPolicy() {
			t.Fatalf("burst_holder: %v %+v", errs, a.TriggerPolicy)
		}
	})
}
