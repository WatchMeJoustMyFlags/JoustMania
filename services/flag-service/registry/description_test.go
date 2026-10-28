package registry

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/flag-service/baseline"
)

// A registry row of the experiment catalog carries a one-line description in
// game terms; the load fails with a clear error without one, and the length is
// capped so the experiment proposer's prompt stays bounded (#2345).
//
// UC: UC-042 BR-008
func TestParseDescription(t *testing.T) {
	b, err := baseline.Load(filepath.Join(root(t), "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	row := func(scope, desc string) string {
		doc := `flags: [{set: game, key: invincibility_seconds, kind: overlay, scope: [` + scope + `], bounds: {range: [0, 10]}`
		if desc != "" {
			doc += `, description: "` + desc + `"`
		}
		return doc + `}]`
	}

	t.Run("UC-042/BR-008 an experiment+arm row without a description fails at load", func(t *testing.T) {
		for _, desc := range []string{"", "   "} {
			_, err := Parse([]byte(row("experiment+arm, real", desc)), b)
			if err == nil || !strings.Contains(err.Error(), "needs a description") || !strings.Contains(err.Error(), "game/invincibility_seconds") {
				t.Fatalf("description %q: err = %v, want a 'needs a description' error naming the row", desc, err)
			}
		}
	})

	t.Run("UC-042/BR-008 a description over the cap fails at load", func(t *testing.T) {
		_, err := Parse([]byte(row("experiment+arm", strings.Repeat("x", MaxDescription+1))), b)
		if err == nil || !strings.Contains(err.Error(), "at most") {
			t.Fatalf("err = %v, want the length cap", err)
		}
		if _, err := Parse([]byte(row("experiment+arm", strings.Repeat("x", MaxDescription))), b); err != nil {
			t.Fatalf("a description of exactly %d characters: %v", MaxDescription, err)
		}
	})

	t.Run("UC-042/BR-008 a description is one line", func(t *testing.T) {
		if _, err := Parse([]byte(row("experiment+arm", `two\nlines`)), b); err == nil {
			t.Fatal("a two-line description was accepted")
		}
	})

	t.Run("UC-042/BR-008 the description is optional outside the catalog and kept when given", func(t *testing.T) {
		if _, err := Parse([]byte(row("stack", "")), b); err != nil {
			t.Fatalf("a stack-only row without a description: %v", err)
		}
		r, err := Parse([]byte(row("experiment+arm", "seconds a fighter cannot lose after being seated")), b)
		if err != nil {
			t.Fatal(err)
		}
		if got := r.Get("game", "invincibility_seconds").Description; got != "seconds a fighter cannot lose after being seated" {
			t.Fatalf("Description = %q", got)
		}
	})
}

// parseDescriptionsMD reads the description table of flags.md §13.3 (the rows
// after the "**Descriptions**" paragraph): flag key -> description text.
func parseDescriptionsMD(t *testing.T) map[string]string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(root(t), "..", "..", "docs", "specs", "flags.md"))
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	start := strings.Index(text, "**Descriptions (`description`, #2345")
	if start < 0 {
		t.Fatal("flags.md §13.3 has no description table")
	}
	out := map[string]string{}
	inTable := false
	for _, line := range strings.Split(text[start:], "\n") {
		if !strings.HasPrefix(line, "|") {
			if inTable {
				break
			}
			continue
		}
		inTable = true
		if strings.HasPrefix(line, "|---") || strings.HasPrefix(line, "| Key") {
			continue
		}
		cells := strings.Split(strings.Trim(line, "|"), "|")
		if len(cells) != 2 {
			t.Fatalf("description table row %q: want 2 cells", line)
		}
		key := backticked.FindStringSubmatch(cells[0])
		if key == nil {
			t.Fatalf("description table row %q has no backticked key", line)
		}
		out[key[1]] = strings.TrimSpace(cells[1])
	}
	return out
}

// Every registry description is the same text as its row in the flags.md §13.3
// description table, and every row of the experiment catalog has one, so the
// spec and what the proposer shows cannot drift (#2345).
//
// UC: UC-042 BR-008
func TestDescriptionsMatchFlagsMD(t *testing.T) {
	t.Run("UC-042/BR-008 registry descriptions equal the flags.md §13.3 description table", func(t *testing.T) {
		md := parseDescriptionsMD(t)
		reg := loadReal(t, "")
		seen := map[string]bool{}
		for _, p := range reg.Policies() {
			if p.Description == "" {
				if p.AllowsScope(ScopeExperiment) {
					t.Errorf("%s/%s allows %s but has no description", p.Set, p.Key, ScopeExperiment)
				}
				continue
			}
			if p.Set != "game" {
				t.Errorf("%s/%s: the description table lists game flags only", p.Set, p.Key)
				continue
			}
			seen[p.Key] = true
			want, ok := md[p.Key]
			switch {
			case !ok:
				t.Errorf("game/%s has a description; flags.md §13.3 description table has no row", p.Key)
			case want != p.Description:
				t.Errorf("game/%s: registry %q, flags.md %q", p.Key, p.Description, want)
			}
		}
		for k := range md {
			if !seen[k] {
				t.Errorf("flags.md §13.3 describes %s; registry.yaml has no description for it", k)
			}
		}
		if len(seen) == 0 {
			t.Fatal("no descriptions found")
		}
	})
}
