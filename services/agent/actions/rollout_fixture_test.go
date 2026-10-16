package actions

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// The RolloutWriter tests run on testdata/rollout.json. It must stay a faithful
// copy of the shipped services/flagd/rollout.json (and its ci/ twin), or the
// writer is tested against a schema flagd never serves (#1422).
//
// UC: UC-051 BR-002
func TestRolloutFixtureMatchesShippedFlagFile(t *testing.T) {
	fixture := readJSONFile(t, filepath.Join("testdata", "rollout.json"))
	for _, rel := range []string{"rollout.json", filepath.Join("ci", "rollout.json")} {
		t.Run("UC-051/BR-002 "+rel, func(t *testing.T) {
			shipped := readJSONFile(t, filepath.Join("..", "..", "flagd", rel))
			if !reflect.DeepEqual(fixture, shipped) {
				t.Fatalf("testdata/rollout.json drifted from services/flagd/%s: copy the shipped file over the fixture", rel)
			}
		})
	}
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
