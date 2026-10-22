package registry

import (
	"encoding/json"
	"path/filepath"
	"sort"
	"testing"

	"github.com/joustmania/flag-service/baseline"
)

// The agent's experiment_seed flag (#1907, flags.md §3) takes declared variants
// only, so every seed is a reviewed variant. This test is that review gate for
// the flag-service half: in both baselines every variant other than `off` names
// a `game` key the agent may write per experiment arm (`experiment+arm` scope),
// and its value passes that key's bounds (CheckValue), so the seed's Start is
// not refused by the registry. The agent half (the schema) is
// services/agent TestExperimentSeedFlagVariantsParse.
//
// UC: UC-070 BR-007
func TestExperimentSeedVariants(t *testing.T) {
	for _, dir := range []string{"", "ci"} {
		t.Run("UC-070/BR-007 services/flagd/"+dir+" experiment_seed variants are declarable", func(t *testing.T) {
			b, err := baseline.Load(filepath.Join(root(t), "..", "flagd", dir), baseline.DefaultFiles)
			if err != nil {
				t.Fatal(err)
			}
			reg, err := LoadFile(filepath.Join(root(t), "registry.yaml"), b)
			if err != nil {
				t.Fatal(err)
			}
			seed := b.Flag("agent", "experiment_seed")
			policy := reg.Get("agent", "experiment_seed")
			if seed == nil || policy == nil {
				t.Fatalf("experiment_seed: flag %v, registry row %v; want both", seed != nil, policy != nil)
			}
			declared := seed.VariantNames()
			allowed := append([]string(nil), policy.Bounds.Variants...)
			sort.Strings(declared)
			sort.Strings(allowed)
			if len(declared) != len(allowed) {
				t.Fatalf("registry variants %v, flag variants %v: every variant must be listed", allowed, declared)
			}
			for i := range declared {
				if declared[i] != allowed[i] {
					t.Fatalf("registry variants %v, flag variants %v: every variant must be listed", allowed, declared)
				}
			}
			for name, raw := range seed.Variants {
				var obj map[string]json.RawMessage
				if err := json.Unmarshal(raw, &obj); err != nil {
					t.Fatalf("variant %s is not an object: %v", name, err)
				}
				if name == "off" {
					if len(obj) != 0 {
						t.Fatalf("variant off = %s, want {}", raw)
					}
					continue
				}
				var key string
				if err := json.Unmarshal(obj["flag"], &key); err != nil || key == "" {
					t.Fatalf("variant %s: flag %s is not a non-empty string", name, obj["flag"])
				}
				p := reg.Get("game", key)
				if p == nil || !p.AllowsScope(ScopeExperiment) {
					t.Fatalf("variant %s: game/%s has no registry row with %s scope", name, key, ScopeExperiment)
				}
				gameFlag := b.Flag("game", key)
				if gameFlag == nil {
					t.Fatalf("variant %s: game/%s is not in the baseline", name, key)
				}
				if _, ferr := CheckValue(p, gameFlag, "", obj["value"]); ferr != nil {
					t.Fatalf("variant %s: value %s for game/%s refused: %v", name, obj["value"], key, ferr)
				}
			}
		})
	}
}
