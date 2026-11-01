package main

// C-034 (#1799): the source guard behind TestC034_NoAgentWriterLoosensItsLimits.
// The wire test can only check the writers it drives; this test proves there is
// no other write path. It parses every non-test .go file of the agent (no
// import filter, no build-tag filter) and fails on:
//
//   - any selector named Put, Post or Delete (a call or a method value, on any
//     receiver) outside the known call sites below;
//   - a known flag-service write whose set argument is not a package constant
//     with the value interventions, rollout or game;
//   - a known flag-service write that no row of c034WriterRows drives, or a known
//     call site that no longer exists (stale entry);
//   - a flag-service client built outside its five known sites;
//   - a raw HTTP write verb (http.MethodPut/Post/Patch/Delete, the literal verbs,
//     http.Post/PostForm, an (*http.Client).Do) outside its known sites;
//   - inside flagservice/, a request method other than GET sent by any function
//     besides Client.Put, Client.Post and Client.Delete, or any Put/Post/Delete
//     selector outside those three (a Client wrapper such as a ClearSet calling
//     c.Delete would otherwise hide behind another name).
//
// Call sites are keyed relpath:Recv.Func (relpath:Func for a plain function), so
// a second type's method of the same name in the same file is not "known".
// A new writer means: drive it in c034WriterRows, then add its call site here.
// A Put/Post/Delete or raw write that is not a flag-service write goes in
// c034OtherPostSites or c034RawWriteSites with the reason.

import (
	"go/ast"
	"go/constant"
	"go/parser"
	"go/token"
	"io/fs"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// c034FlagServiceWrites are the agent's flag-service write call sites
// (relpath:Recv.Func). Each must be driven by a c034WriterRows row.
var c034FlagServiceWrites = map[string]bool{
	"actions/writer.go:Writer.put":                                   true,
	"actions/writer.go:Writer.post":                                  true,
	"actions/rollout_writer.go:RolloutWriter.SetControllerCount":     true,
	"experiment/writer.go:Writer.Apply":                              true,
	"experiment/writer.go:Writer.DryRun":                             true,
	"experiment/writer.go:Writer.Strip":                              true,
	"promote/realdefault.go:serviceRealDefaultWriter.SetRealDefault": true,
	"promote/realdefault.go:serviceRealDefaultWriter.revert":         true,
}

// c034OtherPostSites are Put/Post/Delete selectors that are not flag-service writes.
var c034OtherPostSites = map[string]string{
	"decide/backends/openai/openai.go:Backend.attempt": "chathttp.Post: the model API call (base URL from decision.*.base_url)",
}

// c034ClientSites build a flag-service client (flagservice.New / NewFromEnv).
var c034ClientSites = map[string]bool{
	"actions/writer.go:NewWriterFromEnv":                 true,
	"actions/rollout_writer.go:NewRolloutWriterFromEnv":  true,
	"experiment/writer.go:NewWriterFromEnv":              true,
	"promote/realdefault.go:NewRealDefaultWriterFromEnv": true,
	"main.go:run": true,
}

// c034RawWriteSites send a raw HTTP write that is not to the flag service.
var c034RawWriteSites = map[string]string{
	"decide/backends/internal/chathttp/chathttp.go:PostResponse": "the model API POST (decide backends)",
	"patterngame/digest.go:ResolvedFlagDigest":                   "flagd OFREP bulk evaluate: a read over POST",
	"promote/github.go:httpGitHubClient.post":                    "GitHub REST /repos/... (promotion issue/PR)",
}

// c034AllowedSets are the only values a write's set constant may have (C-034).
var c034AllowedSets = map[string]bool{"interventions": true, "rollout": true, "game": true}

// c034FuncName names a declaration for a guard key: Recv.Func for a method
// (pointer and type parameters stripped), Func for a plain function and
// <package-level> for anything else.
func c034FuncName(decl ast.Decl) string {
	fd, ok := decl.(*ast.FuncDecl)
	if !ok {
		return "<package-level>"
	}
	if fd.Recv == nil || len(fd.Recv.List) == 0 {
		return fd.Name.Name
	}
	t := fd.Recv.List[0].Type
	for {
		switch x := t.(type) {
		case *ast.StarExpr:
			t = x.X
			continue
		case *ast.IndexExpr:
			t = x.X
			continue
		case *ast.IndexListExpr:
			t = x.X
			continue
		}
		break
	}
	if id, ok := t.(*ast.Ident); ok {
		return id.Name + "." + fd.Name.Name
	}
	return "?." + fd.Name.Name
}

type c034Site struct {
	key  string // relpath:Recv.Func or relpath:Func
	pos  token.Position
	call *ast.CallExpr // nil for a method value or other reference
	sel  string
}

// c034Scan is what the guard found in the agent's non-test sources.
type c034Scan struct {
	putPostDelete []c034Site
	clients       []c034Site
	rawWrites     []c034Site
	fakeImports   []string
	consts        map[string]map[string]string // dir -> const name -> string value
	errs          []string
}

func c034ScanSources(t *testing.T) *c034Scan {
	t.Helper()
	s := &c034Scan{consts: map[string]map[string]string{}}
	fset := token.NewFileSet()
	type parsed struct {
		rel string
		f   *ast.File
	}
	var files []parsed
	err := filepath.WalkDir(".", func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, err := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if err != nil {
			return err
		}
		files = append(files, parsed{filepath.ToSlash(path), f})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	// Package-level string constants per directory (one package per directory).
	for _, p := range files {
		dir := filepath.ToSlash(filepath.Dir(p.rel))
		if s.consts[dir] == nil {
			s.consts[dir] = map[string]string{}
		}
		for _, decl := range p.f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, name := range vs.Names {
					if i >= len(vs.Values) {
						continue
					}
					if lit, ok := vs.Values[i].(*ast.BasicLit); ok && lit.Kind == token.STRING {
						s.consts[dir][name.Name] = constant.StringVal(constant.MakeFromLiteral(lit.Value, token.STRING, 0))
					}
				}
			}
		}
	}
	for _, p := range files {
		inFlagService := strings.HasPrefix(p.rel, "flagservice/")
		for _, imp := range p.f.Imports {
			if path, _ := strconv.Unquote(imp.Path.Value); strings.HasSuffix(path, "/flagservice/fake") && !strings.HasPrefix(p.rel, "flagservice/fake/") {
				s.fakeImports = append(s.fakeImports, p.rel)
			}
		}
		if inFlagService {
			if !strings.HasPrefix(p.rel, "flagservice/fake/") {
				s.checkFlagServiceMethods(fset, p.rel, p.f)
			}
			continue
		}
		for _, decl := range p.f.Decls {
			key := p.rel + ":" + c034FuncName(decl)
			calls := map[*ast.SelectorExpr]*ast.CallExpr{}
			ast.Inspect(decl, func(n ast.Node) bool {
				if c, ok := n.(*ast.CallExpr); ok {
					if sel, ok := c.Fun.(*ast.SelectorExpr); ok {
						calls[sel] = c
					}
				}
				return true
			})
			ast.Inspect(decl, func(n ast.Node) bool {
				switch x := n.(type) {
				case *ast.SelectorExpr:
					site := c034Site{key: key, pos: fset.Position(x.Pos()), call: calls[x], sel: x.Sel.Name}
					pkg, _ := x.X.(*ast.Ident)
					switch {
					case x.Sel.Name == "Put" || x.Sel.Name == "Post" || x.Sel.Name == "Delete":
						s.putPostDelete = append(s.putPostDelete, site)
					case pkg != nil && pkg.Name == "flagservice" && (x.Sel.Name == "New" || x.Sel.Name == "NewFromEnv"):
						s.clients = append(s.clients, site)
					case pkg != nil && pkg.Name == "http" && (x.Sel.Name == "MethodPut" || x.Sel.Name == "MethodPost" ||
						x.Sel.Name == "MethodPatch" || x.Sel.Name == "MethodDelete" || x.Sel.Name == "PostForm"):
						s.rawWrites = append(s.rawWrites, site)
					case x.Sel.Name == "Do" && site.call != nil && len(site.call.Args) == 1:
						if _, isFunc := site.call.Args[0].(*ast.FuncLit); !isFunc { // sync.Once.Do takes a func literal
							s.rawWrites = append(s.rawWrites, site)
						}
					}
				case *ast.BasicLit:
					if x.Kind == token.STRING {
						switch v, _ := strconv.Unquote(x.Value); v {
						case "PUT", "POST", "PATCH", "DELETE":
							s.rawWrites = append(s.rawWrites, c034Site{key: key, pos: fset.Position(x.Pos()), sel: v})
						}
					}
				}
				return true
			})
		}
	}
	return s
}

// checkFlagServiceMethods asserts that inside flagservice/ only Client.Put,
// Client.Post and Client.Delete send a request method other than GET, and that
// no other function there references Put, Post or Delete: a Client wrapper
// (ClearSet -> c.Delete(ctx, "agent", ...)) would otherwise be a write path the
// call-site scan never sees, because flagservice/ is skipped there.
func (s *c034Scan) checkFlagServiceMethods(fset *token.FileSet, rel string, f *ast.File) {
	allowed := map[string]bool{"Client.Put": true, "Client.Post": true, "Client.Delete": true}
	for _, decl := range f.Decls {
		fn := c034FuncName(decl)
		where := rel + ":" + fn
		ast.Inspect(decl, func(n ast.Node) bool {
			switch x := n.(type) {
			case *ast.CallExpr:
				sel, ok := x.Fun.(*ast.SelectorExpr)
				if !ok || sel.Sel.Name != "do" || len(x.Args) < 2 {
					return true
				}
				if m, ok := x.Args[1].(*ast.SelectorExpr); ok && m.Sel.Name == "MethodGet" {
					return true
				}
				if !allowed[fn] {
					s.errs = append(s.errs, fset.Position(x.Pos()).String()+": "+where+" sends a non-GET flag-service request; only Client.Put/Post/Delete may")
				}
			case *ast.BasicLit:
				if v, _ := strconv.Unquote(x.Value); x.Kind == token.STRING && (v == "PUT" || v == "POST" || v == "PATCH" || v == "DELETE") {
					s.errs = append(s.errs, fset.Position(x.Pos()).String()+": "+where+" uses the literal verb "+v)
				}
			case *ast.SelectorExpr:
				switch x.Sel.Name {
				case "MethodPut", "MethodPost", "MethodDelete", "MethodPatch":
					if !allowed[fn] {
						s.errs = append(s.errs, fset.Position(x.Pos()).String()+": "+where+" uses http."+x.Sel.Name+"; only Client.Put/Post/Delete may")
					}
				case "Put", "Post", "Delete":
					if !allowed[fn] {
						s.errs = append(s.errs, fset.Position(x.Pos()).String()+": "+where+" references "+x.Sel.Name+"; a flag-service write must live at an agent call site (c034FlagServiceWrites), not in a Client wrapper")
					}
				}
			}
			return true
		})
	}
}

// TestC034_EveryAgentWriterIsKnownAndDriven is the source guard (C-034, #1799).
func TestC034_EveryAgentWriterIsKnownAndDriven(t *testing.T) {
	s := c034ScanSources(t)
	for _, e := range s.errs {
		t.Errorf("C-034 VIOLATION: %s", e)
	}
	for _, f := range s.fakeImports {
		t.Errorf("C-034 VIOLATION: %s imports the flag-service fake outside tests", f)
	}

	seenWrites := map[string]bool{}
	seenOther := map[string]bool{}
	for _, site := range s.putPostDelete {
		if _, ok := c034OtherPostSites[site.key]; ok {
			seenOther[site.key] = true
			continue
		}
		if !c034FlagServiceWrites[site.key] {
			t.Errorf("C-034 VIOLATION: %s: unknown %s at %s; a flag-service write: drive it in c034WriterRows and list it in c034FlagServiceWrites; anything else (a model API call, a map or cache Delete): list it in c034OtherPostSites with the reason", site.pos, site.sel, site.key)
			continue
		}
		seenWrites[site.key] = true
		if site.call == nil {
			t.Errorf("C-034 VIOLATION: %s: %s is used as a method value at %s; call it directly so its set argument is checked", site.pos, site.sel, site.key)
			continue
		}
		if len(site.call.Args) != 4 {
			t.Errorf("C-034 VIOLATION: %s: %s at %s has %d arguments, want (ctx, set, flag, body)", site.pos, site.sel, site.key, len(site.call.Args))
			continue
		}
		ident, ok := site.call.Args[1].(*ast.Ident)
		if !ok {
			t.Errorf("C-034 VIOLATION: %s: the set argument of %s at %s is not a package constant", site.pos, site.sel, site.key)
			continue
		}
		dir := filepath.ToSlash(filepath.Dir(strings.SplitN(site.key, ":", 2)[0]))
		val, ok := s.consts[dir][ident.Name]
		if !ok {
			t.Errorf("C-034 VIOLATION: %s: the set argument %s of %s at %s is not a package string constant", site.pos, ident.Name, site.sel, site.key)
			continue
		}
		if !c034AllowedSets[val] {
			t.Errorf("C-034 VIOLATION: %s: %s at %s writes set %s = %q; only interventions, rollout and game are allowed", site.pos, site.sel, site.key, ident.Name, val)
		}
	}
	for key := range c034FlagServiceWrites {
		if !seenWrites[key] {
			t.Errorf("stale C-034 entry: no flag-service write at %s any more", key)
		}
	}
	for key := range c034OtherPostSites {
		if !seenOther[key] {
			t.Errorf("stale C-034 entry: no Put/Post/Delete at %s any more", key)
		}
	}

	// Every known write is driven by a wire row, and every row drives known writes.
	driven := map[string]string{}
	for _, row := range c034WriterRows {
		for _, key := range row.drives {
			driven[key] = row.name
			if !c034FlagServiceWrites[key] {
				t.Errorf("wire row %s drives %s, which is not a known write call site", row.name, key)
			}
		}
	}
	var undriven []string
	for key := range c034FlagServiceWrites {
		if _, ok := driven[key]; !ok {
			undriven = append(undriven, key)
		}
	}
	sort.Strings(undriven)
	for _, key := range undriven {
		t.Errorf("C-034 VIOLATION: the write at %s is driven by no c034WriterRows row", key)
	}

	seenClients := map[string]bool{}
	for _, site := range s.clients {
		if !c034ClientSites[site.key] {
			t.Errorf("C-034 VIOLATION: %s: a flag-service client is built at %s, outside the known sites", site.pos, site.key)
		}
		seenClients[site.key] = true
	}
	for key := range c034ClientSites {
		if !seenClients[key] {
			t.Errorf("stale C-034 entry: no flag-service client built at %s any more", key)
		}
	}

	seenRaw := map[string]bool{}
	for _, site := range s.rawWrites {
		if _, ok := c034RawWriteSites[site.key]; !ok {
			t.Errorf("C-034 VIOLATION: %s: raw HTTP write %s at %s; agent flag writes go through flagservice.Client only; a write that is not to the flag service (or a false match such as once.Do(namedFunc) or a POST string literal) goes in c034RawWriteSites with the reason", site.pos, site.sel, site.key)
		}
		seenRaw[site.key] = true
	}
	for key := range c034RawWriteSites {
		if !seenRaw[key] {
			t.Errorf("stale C-034 entry: no raw HTTP write at %s any more", key)
		}
	}
}
