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

// The check is applied to the rest of the tree: no code under services/agent
// may call anthropic.NewClient (or import the SDK's option package) outside
// this package, so every SDK client gets the no-redirect HTTP client, retries
// off and the shared options. There is no OpenAI SDK in the module; if one is
// added, extend banned below.
// UC: UC-044 BR-003
// UC: C-016
func TestNoRawSDKClientConstruction(t *testing.T) {
	root, err := filepath.Abs("../..")
	if err != nil {
		t.Fatal(err)
	}
	banned := map[string]bool{"anthropic.NewClient": true, "openai.NewClient": true}
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
		file, err := parser.ParseFile(token.NewFileSet(), path, src, 0)
		if err != nil {
			return err
		}
		scanned++
		ast.Inspect(file, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			if id, ok := sel.X.(*ast.Ident); ok && banned[id.Name+"."+sel.Sel.Name] {
				rel, _ := filepath.Rel(root, path)
				t.Errorf("%s calls %s.%s: build SDK clients with anthropichttp.NewClient", rel, id.Name, sel.Sel.Name)
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if scanned < 20 {
		t.Fatalf("scanned only %d Go files: the walk root is wrong", scanned)
	}
}
