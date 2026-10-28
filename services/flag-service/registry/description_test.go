package registry

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/joustmania/flag-service/baseline"
)

// A registry row may carry an optional one-line description of at most
// MaxDescription characters; an unknown-field registry still fails (#2345).
//
// UC: UC-042 BR-008
func TestParseDescription(t *testing.T) {
	b, err := baseline.Load(filepath.Join(root(t), "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	row := func(desc string) string {
		doc := `flags: [{set: game, key: invincibility_seconds, kind: overlay, scope: [experiment+arm], bounds: {range: [0, 10]}`
		if desc != "" {
			doc += `, description: "` + desc + `"`
		}
		return doc + `}]`
	}

	t.Run("UC-042/BR-008 a row with a description loads and keeps it", func(t *testing.T) {
		r, err := Parse([]byte(row("seconds a fighter cannot lose")), b)
		if err != nil {
			t.Fatal(err)
		}
		if got := r.Get("game", "invincibility_seconds").Description; got != "seconds a fighter cannot lose" {
			t.Fatalf("Description = %q", got)
		}
	})

	t.Run("UC-042/BR-008 a row without a description still loads", func(t *testing.T) {
		if _, err := Parse([]byte(row("")), b); err != nil {
			t.Fatal(err)
		}
	})

	t.Run("UC-042/BR-008 a description over the cap fails naming the row", func(t *testing.T) {
		_, err := Parse([]byte(row(strings.Repeat("x", MaxDescription+1))), b)
		if err == nil || !strings.Contains(err.Error(), "at most") || !strings.Contains(err.Error(), "game/invincibility_seconds") {
			t.Fatalf("err = %v, want the length cap naming the row", err)
		}
		if _, err := Parse([]byte(row(strings.Repeat("x", MaxDescription))), b); err != nil {
			t.Fatalf("a description of exactly %d characters: %v", MaxDescription, err)
		}
	})

	t.Run("UC-042/BR-008 a multi-line or blank description fails", func(t *testing.T) {
		for _, d := range []string{`two\nlines`, `   `} {
			if _, err := Parse([]byte(row(d)), b); err == nil {
				t.Fatalf("description %q was accepted", d)
			}
		}
	})
}
