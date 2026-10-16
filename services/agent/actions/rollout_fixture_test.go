package actions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// The RolloutWriter tests run on testdata/rollout.json. It must stay a faithful
// copy of the shipped services/flagd/rollout.json, or the writer is tested
// against a schema flagd never serves (#1422). ci/rollout.json may override
// defaults (same policy as lib/tests/test_flagd_ci_drift.py), so against it
// only the schema is compared: flag keys and each flag's variants map.
//
// UC: UC-051 BR-001 BR-002
func TestRolloutFixtureMatchesShippedFlagFile(t *testing.T) {
	fixture := readJSONFile(t, filepath.Join("testdata", "rollout.json"))

	t.Run("UC-051/BR-002 base rollout.json exact", func(t *testing.T) {
		shipped := readJSONFile(t, filepath.Join("..", "..", "flagd", "rollout.json"))
		if !reflect.DeepEqual(fixture, shipped) {
			t.Fatal("testdata/rollout.json drifted from services/flagd/rollout.json: copy the shipped file over the fixture")
		}
	})

	t.Run("UC-051/BR-001 ci/rollout.json schema", func(t *testing.T) {
		shipped := readJSONFile(t, filepath.Join("..", "..", "flagd", "ci", "rollout.json"))
		want, got := flagSchema(t, fixture), flagSchema(t, shipped)
		if !reflect.DeepEqual(want, got) {
			t.Fatalf("testdata/rollout.json schema (flag keys + variants) drifted from services/flagd/ci/rollout.json:\nfixture: %v\nci:      %v", want, got)
		}
	})
}

// flagSchema reduces a flag file to flag key -> variants map, ignoring
// defaultVariant, state and targeting.
func flagSchema(t *testing.T, doc any) map[string]any {
	t.Helper()
	root, ok := doc.(map[string]any)
	if !ok {
		t.Fatalf("flag file root is %T, want object", doc)
	}
	flags, ok := root["flags"].(map[string]any)
	if !ok {
		t.Fatalf("flag file has no flags object")
	}
	schema := make(map[string]any, len(flags))
	for key, raw := range flags {
		flag, _ := raw.(map[string]any)
		schema[key] = flag["variants"]
	}
	return schema
}

func readJSONFile(t *testing.T, path string) any {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	return doc
}
