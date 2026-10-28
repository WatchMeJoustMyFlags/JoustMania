package anthropichttp_test

import (
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/internal/anthropichttp"
)

// crossHostRedirect starts a redirector that 307s to a second server and
// returns the redirector URL plus a pointer to what the target received.
func crossHostRedirect(t *testing.T) (redirectorURL string, targetHits *[]http.Header) {
	t.Helper()
	hits := &[]http.Header{}
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*hits = append(*hits, r.Header.Clone())
	}))
	t.Cleanup(target.Close)
	redirector := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+r.URL.Path, http.StatusTemporaryRedirect)
	}))
	t.Cleanup(redirector.Close)
	return redirector.URL, hits
}

func assertRefusesRedirect(t *testing.T, client *http.Client) {
	t.Helper()
	url, hits := crossHostRedirect(t)
	req, err := http.NewRequest(http.MethodPost, url+"/v1/x", strings.NewReader("{}"))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("X-Api-Key", "sk-secret")
	req.Header.Set("Authorization", "Bearer sk-secret")
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("a refused redirect is the response, not an error: %v", err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusTemporaryRedirect {
		t.Fatalf("status = %d, want the 307 returned as the response", resp.StatusCode)
	}
	if len(*hits) != 0 {
		t.Fatalf("redirect target received %d request(s) with headers %v", len(*hits), *hits)
	}
}

// UC: UC-044 BR-003
// UC: C-016
func TestNewHTTPClient_RefusesCrossHostRedirect(t *testing.T) {
	assertRefusesRedirect(t, anthropichttp.NewHTTPClient(time.Second))
	assertRefusesRedirect(t, anthropichttp.NewHTTPClient(0))
}

// sdkImportPaths are the SDK modules whose NewClient must be built here only.
// The agent module has no OpenAI SDK; the path is listed so adding one is caught.
var sdkImportPaths = map[string]bool{
	"github.com/anthropics/anthropic-sdk-go": true,
	"github.com/openai/openai-go":            true,
}

// rawClientCalls returns one message per raw SDK client construction in src:
// <local>.NewClient where <local> is the file's own name for an SDK import
// (so an alias is caught), and any dot import of an SDK package. Importing the
// SDK (or its option package) without calling NewClient is legitimate.
func rawClientCalls(t *testing.T, name string, src []byte) []string {
	t.Helper()
	file, err := parser.ParseFile(token.NewFileSet(), name, src, 0)
	if err != nil {
		t.Fatal(err)
	}
	locals := map[string]bool{}
	var found []string
	for _, imp := range file.Imports {
		path := strings.Trim(imp.Path.Value, `"`)
		base := strings.TrimSuffix(path, "/")
		if i := strings.LastIndex(base, "/"); i >= 0 {
			base = base[i+1:]
		}
		// Sub-packages (option, ...) are not clients; only the root is.
		isSDK := sdkImportPaths[path]
		if !isSDK {
			continue
		}
		switch {
		case imp.Name == nil:
			locals[base] = true
		case imp.Name.Name == ".":
			found = append(found, name+" dot-imports "+path+": build SDK clients with anthropichttp.NewClient")
		case imp.Name.Name != "_":
			locals[imp.Name.Name] = true
		}
	}
	ast.Inspect(file, func(n ast.Node) bool {
		sel, ok := n.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		if id, ok := sel.X.(*ast.Ident); ok && locals[id.Name] && sel.Sel.Name == "NewClient" {
			found = append(found, name+" calls "+id.Name+".NewClient: build SDK clients with anthropichttp.NewClient")
		}
		return true
	})
	return found
}

// UC: UC-044 BR-003
// UC: C-016
func TestRawClientCalls_FlagsAliasAndDotImport(t *testing.T) {
	aliased := []byte(`package x
import sdk "github.com/anthropics/anthropic-sdk-go"
func f() { _ = sdk.NewClient() }
`)
	if got := rawClientCalls(t, "aliased.go", aliased); len(got) != 1 {
		t.Fatalf("aliased NewClient not flagged: %v", got)
	}
	dot := []byte(`package x
import . "github.com/anthropics/anthropic-sdk-go"
func f() { _ = NewClient() }
`)
	if got := rawClientCalls(t, "dot.go", dot); len(got) != 1 {
		t.Fatalf("dot import not flagged: %v", got)
	}
	plain := []byte(`package x
import (
	"github.com/anthropics/anthropic-sdk-go"
	"github.com/anthropics/anthropic-sdk-go/option"
)
var _ anthropic.Client
var _ = option.WithAPIKey
`)
	if got := rawClientCalls(t, "plain.go", plain); len(got) != 0 {
		t.Fatalf("import without NewClient flagged: %v", got)
	}
}

// The check is applied to the rest of the tree: no code under services/agent
// may construct an SDK client (anthropic.NewClient, under any import alias, or
// a dot import of the SDK) outside this package, so every SDK client gets the
// no-redirect HTTP client, retries off and the shared options. The option
// package alone is not a client and is not flagged.
// UC: UC-044 BR-003
// UC: C-016
func TestNoRawSDKClientConstruction(t *testing.T) {
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	ownDir := filepath.Join(root, "internal", "anthropichttp")
	var scanned int
	err = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if path == ownDir || d.Name() == "vendor" || d.Name() == "node_modules" {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}
		src, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		scanned++
		rel, _ := filepath.Rel(root, path)
		for _, msg := range rawClientCalls(t, rel, src) {
			t.Error(msg)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if scanned < 20 {
		t.Fatalf("scanned only %d Go files: the walk root is wrong", scanned)
	}
}
