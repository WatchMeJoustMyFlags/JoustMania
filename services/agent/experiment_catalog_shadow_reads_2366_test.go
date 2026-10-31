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
// (`sensitivity` left it with #2392: the shadow spawner resolves it per arm.)
var catalogShadowGaps = map[string]string{}

// baseReadUses names, for a mode-restricted key games/base.py reads for every mode,
// the attribute the read sets: the key acts only where a mode uses that attribute
// (#2392). A restricted key base.py reads without an entry here fails the guard.
var baseReadUses = map[string]string{
	"death_grace_period_seconds": "death_grace_period",
}

var (
	factoryEntry = regexp.MustCompile(`Games\.(\w+):\s*(\w+)\.(\w+)`)
	classDef     = regexp.MustCompile(`(?m)^class (\w+)\((\w+)\):`)
)

// coordinatorModeFiles returns, per game mode of the coordinator's _GAME_CLASSES
// (game_factory.py), the games/*.py files its class hierarchy runs: the mode's own
// module and every base class module up to and including base.py and its mixin shields.py.
func coordinatorModeFiles(t *testing.T) map[string]map[string]bool {
	t.Helper()
	gamesDir := filepath.Join("..", "game_coordinator", "games")
	files, err := filepath.Glob(filepath.Join(gamesDir, "*.py"))
	if err != nil {
		t.Fatal(err)
	}
	classFile := map[string]string{} // class -> file name
	classBase := map[string]string{} // class -> base class
	for _, f := range files {
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range classDef.FindAllStringSubmatch(string(src), -1) {
			classFile[m[1]] = filepath.Base(f)
			classBase[m[1]] = m[2]
		}
	}
	factory, err := os.ReadFile(filepath.Join("..", "game_coordinator", "game_factory.py"))
	if err != nil {
		t.Fatal(err)
	}
	modes := map[string]map[string]bool{}
	for _, m := range factoryEntry.FindAllStringSubmatch(string(factory), -1) {
		mode, class := m[1], m[3]
		// shields.py holds BaseGameMode's ShieldMixin (#2360): it runs in every mode, as base.py does.
		path := map[string]bool{"base.py": true, "shields.py": true}
		for c := class; c != ""; c = classBase[c] {
			if f, ok := classFile[c]; ok {
				path[f] = true
			}
			if len(path) > 10 {
				break
			}
		}
		modes[mode] = path
	}
	if len(modes) == 0 || modes["JoustFFA"] == nil || !modes["JoustFFA"]["ffa.py"] {
		t.Fatalf("no JoustFFA in game_factory.py _GAME_CLASSES: the scan pattern is stale (%v)", modes)
	}
	return modes
}

// onModePath reports whether a coordinator source file runs in a game of a mode
// with the given games/ files: every file outside games/, and the mode's own files.
func onModePath(path string, modeFiles map[string]bool) bool {
	if filepath.Base(filepath.Dir(path)) != "games" {
		return true
	}
	return modeFiles[filepath.Base(path)]
}

// attributeUseFiles returns the coordinator files (non-test) that use .<attr> in
// code other than assigning it (comments do not count).
func attributeUseFiles(t *testing.T, attr string) []string {
	t.Helper()
	use := regexp.MustCompile(`\.` + regexp.QuoteMeta(attr) + `\b(\s*=[^=])?`)
	var out []string
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
		for _, line := range strings.Split(string(src), "\n") {
			code, _, _ := strings.Cut(line, "#") // a comment is not a use
			if slices.ContainsFunc(use.FindAllStringSubmatch(code, -1), func(m []string) bool {
				return m[1] == "" // a use, not an assignment
			}) {
				out = append(out, path)
				break
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
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
// path names (services/agent/gamerunner and shadow_spawner.go, non-test): a key
// resolved there with the arm's evaluation context goes into the shadow
// StartGameConfig (sensitivity, #2392).
func shadowStartConfigKeys(t *testing.T, catalog []string) map[string]bool {
	t.Helper()
	files, err := filepath.Glob(filepath.Join("gamerunner", "*.go"))
	if err != nil {
		t.Fatal(err)
	}
	files = append(files, "shadow_spawner.go")
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
// experiment+arm, flags.md §13.3) that the declarer and the proposer offer for a
// shadow game of mode M (offeredInShadowMode) must be read on M's path: the
// coordinator reads it in the game's flag context outside games/ or in M's class
// hierarchy, or the agent resolves it into the shadow StartGameConfig. A key only
// the menu reads gives two identical arms (#2366). A key catalogKeyModes restricts
// to some modes must act only in those modes, so the restriction never hides a key
// that would change a shadow game (#2392).
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
	modes := coordinatorModeFiles(t)
	shadowStart := shadowStartConfigKeys(t, catalog)
	readOnPath := func(key string, modeFiles map[string]bool) bool {
		return shadowStart[key] || slices.ContainsFunc(coordinatorReads[key], func(p string) bool {
			return onModePath(p, modeFiles)
		})
	}

	t.Run("UC-043/BR-009 every key offered for a shadow mode is read on that mode's path", func(t *testing.T) {
		if !slices.Contains(catalog, shadowSensitivityKey) || !shadowStart[shadowSensitivityKey] {
			t.Errorf("%s is not resolved into the shadow StartGameConfig (#2392)", shadowSensitivityKey)
		}
		for mode, files := range modes {
			for _, key := range catalog {
				if !offeredInShadowMode(key, mode) || readOnPath(key, files) {
					continue
				}
				if issue, ok := catalogShadowGaps[key]; ok {
					t.Logf("%s: known gap %s", key, issue)
					continue
				}
				t.Errorf("game/%s is offered for a %s shadow game but no %s game reads it: "+
					"read it in the coordinator's game flag context or resolve it into the shadow "+
					"StartGameConfig, restrict it to the modes that read it in catalogKeyModes, "+
					"or remove experiment+arm from its registry row", key, mode, mode)
			}
		}
	})
	t.Run("UC-043/BR-009 a known gap is still a catalog key that does not reach the shadow game", func(t *testing.T) {
		for key, issue := range catalogShadowGaps {
			if !slices.Contains(catalog, key) {
				t.Errorf("known gap %s (%s) is no longer in the experiment catalog: remove the entry", key, issue)
			}
			if readOnPath(key, modes["JoustFFA"]) {
				t.Errorf("known gap %s (%s) now reaches the shadow game: remove the entry", key, issue)
			}
		}
	})
	t.Run("UC-043/BR-009 a mode-restricted key acts only in its modes", func(t *testing.T) {
		for key, listed := range catalogKeyModes {
			if !slices.Contains(catalog, key) {
				t.Errorf("catalogKeyModes lists %s, which is not in the experiment catalog: remove the entry", key)
				continue
			}
			inListed := func(path string) bool {
				return slices.ContainsFunc(listed, func(m string) bool { return onModePath(path, modes[m]) })
			}
			for _, m := range listed {
				if modes[m] == nil {
					t.Errorf("catalogKeyModes lists %s for mode %s, which game_factory.py does not run", key, m)
				}
			}
			if shadowStart[key] {
				t.Errorf("%s is resolved into every shadow StartGameConfig: remove it from catalogKeyModes", key)
			}
			reads := coordinatorReads[key]
			if len(reads) == 0 {
				t.Errorf("%s is restricted in catalogKeyModes but no game mode reads it", key)
			}
			for _, path := range reads {
				if filepath.Base(path) == "base.py" {
					attr, ok := baseReadUses[key]
					if !ok {
						t.Errorf("games/base.py reads %s for every mode: name the attribute it sets in "+
							"baseReadUses or remove it from catalogKeyModes", key)
						continue
					}
					uses := attributeUseFiles(t, attr)
					if len(uses) == 0 {
						t.Errorf("no mode uses %s (set from %s): remove the key from catalogKeyModes or baseReadUses", attr, key)
					}
					for _, use := range uses {
						if filepath.Base(use) == "base.py" || !inListed(use) {
							t.Errorf("%s (set from %s) is used in %s, outside the modes %v catalogKeyModes lists",
								attr, key, use, listed)
						}
						for mode, files := range modes {
							if onModePath(use, files) && !slices.Contains(listed, mode) {
								t.Errorf("%s (set from %s) is used in %s, which a %s game runs too: add %s to its catalogKeyModes entry",
									attr, key, use, mode, mode)
							}
						}
					}
					continue
				}
				if !inListed(path) {
					t.Errorf("%s is read in %s, outside the modes %v catalogKeyModes lists", key, path, listed)
				}
				for mode, files := range modes {
					if onModePath(path, files) && !slices.Contains(listed, mode) {
						t.Errorf("%s is read in %s, which a %s game runs too: add %s to its catalogKeyModes entry",
							key, path, mode, mode)
					}
				}
			}
		}
	})
}
