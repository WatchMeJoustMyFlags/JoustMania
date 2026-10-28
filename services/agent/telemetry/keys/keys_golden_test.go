package keys

import (
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// TestKeysGolden pins every exported name=value pair of keys.go against
// testdata/keys.golden. The values are the telemetry contract (docs/specs/telemetry.md):
// dashboards and the spec reference them by string, so a changed, added or removed
// constant must be a deliberate edit of the golden file too.
func TestKeysGolden(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, "keys.go", nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, decl := range file.Decls {
		gen, ok := decl.(*ast.GenDecl)
		if !ok || gen.Tok != token.CONST {
			continue
		}
		for _, spec := range gen.Specs {
			vs := spec.(*ast.ValueSpec)
			for i, name := range vs.Names {
				lit, ok := vs.Values[i].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					t.Fatalf("%s is not a plain string literal", name.Name)
				}
				val, err := strconv.Unquote(lit.Value)
				if err != nil {
					t.Fatal(err)
				}
				got = append(got, name.Name+"="+val)
			}
		}
	}
	raw, err := os.ReadFile("testdata/keys.golden")
	if err != nil {
		t.Fatal(err)
	}
	var want []string
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		want = append(want, line)
	}
	sort.Strings(got)
	sort.Strings(want)
	if len(got) != len(want) {
		t.Fatalf("keys.go has %d constants, golden has %d", len(got), len(want))
	}
	for i := range got {
		if got[i] != want[i] {
			t.Errorf("constant mismatch: keys.go %q, golden %q", got[i], want[i])
		}
	}
}
