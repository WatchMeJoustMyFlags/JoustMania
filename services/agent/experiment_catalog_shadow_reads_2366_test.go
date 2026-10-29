package main

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strings"
	"testing"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice/fake"
)

// catalogShadowGaps are experiment-catalog keys known NOT to reach the shadow game,
// each with its open issue. Keep it empty: an entry is a known bug, not an exemption.
var catalogShadowGaps = map[string]string{
	// Read only by the menu into StartGameConfig.sensitivity; the shadow start uses
	// SHADOW_GAME_SENSITIVITY (#2392).
	"sensitivity": "#2392",
}

// coordinatorFlagRead matches a game-set flag read in the game coordinator:
// read_<kind>_flag("game", "<key>", ...) and read_object_flag_variant("game", "<key>", ...),
// the reads that run in the game's flag context (game_kind, experiment_id, arm).
var coordinatorFlagRead = regexp.MustCompile(`read_\w+_flag(?:_variant)?\(\s*"game",\s*"([^"]+)"`)

// coordinatorReadGameKeys returns every game flag the coordinator's non-test Python
// sources read through the flag helpers.
func coordinatorReadGameKeys(t *testing.T) map[string]bool {
	t.Helper()
	keys := map[string]bool{}
	root := filepath.Join("..", "game_coordinator")
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case "tests", ".venv", "__pycache__":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".py") {
			return nil
		}
		src, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		for _, m := range coordinatorFlagRead.FindAllStringSubmatch(string(src), -1) {
			keys[m[1]] = true
		}
		return nil
	})
	if err != nil {
		t.Fatalf("scan coordinator sources: %v", err)
	}
	if len(keys) == 0 {
		t.Fatal("no coordinator flag read found: the scan pattern is stale")
	}
	return keys
}

// shadowStartConfigKeys returns every game flag key the agent's shadow-game start
// path names (services/agent/gamerunner, non-test): a key resolved there with the
// arm's evaluation context goes into the shadow StartGameConfig.
func shadowStartConfigKeys(t *testing.T, catalog []string) map[string]bool {
	t.Helper()
	files, err := filepath.Glob(filepath.Join("gamerunner", "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	var src strings.Builder
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		src.Write(b)
	}
	keys := map[string]bool{}
	for _, key := range catalog {
		if strings.Contains(src.String(), `"`+key+`"`) {
			keys[key] = true
		}
	}
	return keys
}

// Every key of the experiment catalog (a game registry row with scope
// experiment+arm, flags.md §13.3) must reach the shadow game an experiment arm
// runs: the coordinator reads it in the game's flag context, or the agent resolves
// it into the shadow StartGameConfig. A key only the menu reads gives two identical
// arms (#2366).
//
// UC: UC-043 BR-009
func TestUC043_BR009_CatalogKeysReachTheShadowGame(t *testing.T) {
	policies, err := fake.RegistryPolicies(filepath.Join("..", "flag-service", "registry.yaml"), experiment.GameFlagSet)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	var catalog []string
	for key, p := range policies {
		if slices.Contains(p.Scopes, experimentScope) {
			catalog = append(catalog, key)
		}
	}
	sort.Strings(catalog)
	if len(catalog) == 0 {
		t.Fatal("empty experiment catalog: registry not read")
	}
	coordinator := coordinatorReadGameKeys(t)
	shadowStart := shadowStartConfigKeys(t, catalog)

	t.Run("UC-043/BR-009 every catalog key is read on the shadow-game path", func(t *testing.T) {
		for _, key := range catalog {
			if coordinator[key] || shadowStart[key] {
				continue
			}
			if issue, ok := catalogShadowGaps[key]; ok {
				t.Logf("%s: known gap %s", key, issue)
				continue
			}
			t.Errorf("game/%s is in the experiment catalog but no shadow game reads it: "+
				"read it in the coordinator's game flag context or resolve it into the shadow "+
				"StartGameConfig, or remove experiment+arm from its registry row", key)
		}
	})
	t.Run("UC-043/BR-009 a known gap is still a catalog key that does not reach the shadow game", func(t *testing.T) {
		for key, issue := range catalogShadowGaps {
			if !slices.Contains(catalog, key) {
				t.Errorf("known gap %s (%s) is no longer in the experiment catalog: remove the entry", key, issue)
			}
			if coordinator[key] || shadowStart[key] {
				t.Errorf("known gap %s (%s) now reaches the shadow game: remove the entry", key, issue)
			}
		}
	})
}
