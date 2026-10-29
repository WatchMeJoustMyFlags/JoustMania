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

// catalogModeOnlyGaps are experiment-catalog keys the coordinator reads only inside
// a game mode other than JoustFFA. Shadow games are JoustFFA only (UC-046 BR-003,
// BR-019), so an arm value on them never reaches a shadow game. The maintainer
// decision on these keys is pending in #2392; an entry is a known gap, not an
// exemption, and goes when the key leaves the catalog or a shadow game reads it.
var catalogModeOnlyGaps = map[string]string{
	"nonstop.respawn_seconds":                 "#2392",
	"nonstop.spawn_protection_seconds":        "#2392",
	"fight_club.round_seconds":                "#2392",
	"tournament.match_seconds":                "#2392",
	"tournament.time_between_matches_seconds": "#2392",
	"werewolf.werewolf_fraction":              "#2392",
	"zombie.initial_count":                    "#2392",
	"zombie.respawn_min_seconds":              "#2392",
	"zombie.respawn_max_seconds":              "#2392",
}

// shadowModeFiles are the game-mode sources a JoustFFA shadow game runs: a read in
// any other games/<mode>.py runs only in that mode.
var shadowModeFiles = map[string]bool{"ffa.py": true, "base.py": true}

// onShadowPath reports whether a coordinator source file runs in a JoustFFA shadow
// game: every file outside games/, and the FFA mode's own class hierarchy.
func onShadowPath(path string) bool {
	if filepath.Base(filepath.Dir(path)) != "games" {
		return true
	}
	return shadowModeFiles[filepath.Base(path)]
}

// coordinatorFlagRead matches a game-set flag read in the game coordinator:
// read_<kind>_flag("game", "<key>", ...) and read_object_flag_variant("game", "<key>", ...),
// the reads that run in the game's flag context (game_kind, experiment_id, arm).
var coordinatorFlagRead = regexp.MustCompile(`read_\w+_flag(?:_variant)?\(\s*"game",\s*"([^"]+)"`)

// coordinatorReadGameKeys returns every game flag the coordinator's non-test Python
// sources read through the flag helpers, with the source files that read it.
func coordinatorReadGameKeys(t *testing.T) map[string][]string {
	t.Helper()
	keys := map[string][]string{}
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
			keys[m[1]] = append(keys[m[1]], path)
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
// experiment+arm, flags.md §13.3) must be read on the shadow-game path an
// experiment arm runs: the coordinator reads it in the game's flag context outside
// a non-FFA game mode, or the agent resolves it into the shadow StartGameConfig. A
// key only the menu reads gives two identical arms (#2366); a key only a non-FFA
// mode reads is a known gap listed in catalogModeOnlyGaps (#2392).
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
	coordinatorReads := coordinatorReadGameKeys(t)
	coordinator := map[string]bool{} // read on the shadow-game path
	for key, files := range coordinatorReads {
		coordinator[key] = slices.ContainsFunc(files, onShadowPath)
	}
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
			if issue, ok := catalogModeOnlyGaps[key]; ok {
				t.Logf("%s: known gap %s (read only in a non-FFA mode)", key, issue)
				continue
			}
			t.Errorf("game/%s is in the experiment catalog but no shadow game reads it: "+
				"read it in the coordinator's game flag context or resolve it into the shadow "+
				"StartGameConfig (a read only inside a non-FFA game mode does not count), "+
				"or remove experiment+arm from its registry row", key)
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
	t.Run("UC-043/BR-009 a known mode-only gap is still a catalog key read only in a non-FFA mode", func(t *testing.T) {
		for key, issue := range catalogModeOnlyGaps {
			if !slices.Contains(catalog, key) {
				t.Errorf("known gap %s (%s) is no longer in the experiment catalog: remove the entry", key, issue)
			}
			if len(coordinatorReads[key]) == 0 {
				t.Errorf("known gap %s (%s) is no longer read by any game mode: remove the entry", key, issue)
			}
			if coordinator[key] || shadowStart[key] {
				t.Errorf("known gap %s (%s) now reaches the shadow game: remove the entry", key, issue)
			}
		}
	})
}
