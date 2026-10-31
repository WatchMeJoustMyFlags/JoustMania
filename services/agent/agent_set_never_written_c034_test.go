package main

// C-034 (#1799): the agent cannot loosen its own limits. The flag service has no
// write authentication (D11), so the key-level invariant of C-034 holds only by
// construction, and this test is what checks it on the wire. It drives every
// agent writer (actions.Writer, actions.RolloutWriter, the experiment Writer,
// Gate and GateTargetingWriter, the promotion real-default writer and the kill
// switch) against a recording copy of the flag service's routes and asserts, on
// every raw request, the (set, key, scope) C-034 allows:
//
//   - rollout: only current_controller_count, stack scope;
//   - interventions: only the keys of flags.md §13.1, always game-scoped;
//   - game: only an experiment+arm value or a game_kind=real overlay, never
//     stack-wide (the flag-service registry then refuses every key outside the
//     experiment catalog, e.g. shadow_policy or max_shadow_games);
//   - never the `agent` set, never a redirect, an unrouted request or a path
//     with other than two segments.
//
// agent_set_writers_guard_c034_test.go checks that every write call site in the
// agent is one of the writers driven here (c034WriterRows).

import (
	"context"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
	"github.com/joustmania/agent/promote"

	"gopkg.in/yaml.v3"
)

// c034Request is one raw request the recording flag service saw.
type c034Request struct {
	Method      string
	EscapedPath string
	Query       url.Values
	Status      int
	// Set and Flag are the decoded path values of a /{set}/{flag} route ("" otherwise).
	Set, Flag string
	Write     *flagservice.Write
	Event     *flagservice.Event
}

func (r c034Request) String() string {
	return fmt.Sprintf("%s %s?%s -> %d", r.Method, r.EscapedPath, r.Query.Encode(), r.Status)
}

// c034Recorder mirrors the flag service's routes (services/flag-service/api/api.go
// Handler) and records every raw request, routed or not.
type c034Recorder struct {
	mu   sync.Mutex
	reqs []c034Request
	srv  *httptest.Server
}

type c034ReqKey struct{}

// c034BaseFlag is the rendered baseline of every game flag the recorder serves.
const c034BaseFlag = `{"state":"ENABLED","variants":{"low":1,"mid":2,"high":3},"defaultVariant":"mid"}`

func newC034Recorder(t *testing.T) *c034Recorder {
	t.Helper()
	rec := &c034Recorder{}
	mux := http.NewServeMux()
	ok := func(w http.ResponseWriter, _ *http.Request) { c034JSON(w, map[string]any{}) }
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		c034JSON(w, map[string]string{"epoch": "c034-epoch"})
	})
	mux.HandleFunc("GET /{$}", ok)
	mux.HandleFunc("GET /state", ok)
	mux.HandleFunc("GET /games", ok)
	mux.HandleFunc("PUT /games/{gameId}", ok)
	mux.HandleFunc("DELETE /games/{gameId}", ok)
	mux.HandleFunc("PUT /coordinators/{instance}/games", ok)
	mux.HandleFunc("GET /{set}", func(w http.ResponseWriter, r *http.Request) {
		// One promotion overlay, so the kill switch has a flag to revert (#1704).
		c034JSON(w, flagservice.SetView{FlagSet: r.PathValue("set"), Served: true, Flags: []flagservice.FlagView{{
			Flag: "sensitivity", RealOverlay: &flagservice.ValueView{Variant: "real_1", Writer: "promotion"},
		}}})
	})
	mux.HandleFunc("DELETE /{set}", ok)
	mux.HandleFunc("GET /{set}/{flag}", func(w http.ResponseWriter, r *http.Request) {
		c034JSON(w, flagservice.FlagView{
			Flag:             r.PathValue("flag"),
			Mutable:          true,
			Policy:           &flagservice.Policy{Kind: "overlay", Scopes: []string{"stack", "experiment+arm", "real"}},
			BaselineDefault:  "mid",
			EffectiveDefault: "mid",
			Variants:         []string{"low", "mid", "high"},
			VariantValues:    map[string]json.RawMessage{"low": json.RawMessage("1"), "mid": json.RawMessage("2"), "high": json.RawMessage("3")},
			RealOverlay:      &flagservice.ValueView{Variant: "real_1", Writer: "promotion"},
			Rendered:         json.RawMessage(c034BaseFlag),
		})
	})
	mux.HandleFunc("PUT /{set}/{flag}", func(w http.ResponseWriter, r *http.Request) {
		rq := r.Context().Value(c034ReqKey{}).(*c034Request)
		rq.Set, rq.Flag = r.PathValue("set"), r.PathValue("flag")
		var body flagservice.Write
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, `{"error":"malformed"}`, http.StatusBadRequest)
			return
		}
		rq.Write = &body
		res := flagservice.Result{FlagSet: rq.Set, Flag: rq.Flag, Variant: "c034_v1", Epoch: "c034-epoch"}
		if r.URL.Query().Get("dry_run") == "true" {
			res.DryRun = true
			res.RenderedBefore = json.RawMessage(c034BaseFlag)
			res.Rendered = json.RawMessage(`{"state":"ENABLED","variants":{"low":1,"mid":2,"high":3,"c034_v1":3},"defaultVariant":"mid"}`)
		}
		c034JSON(w, res)
	})
	mux.HandleFunc("POST /{set}/{flag}", func(w http.ResponseWriter, r *http.Request) {
		rq := r.Context().Value(c034ReqKey{}).(*c034Request)
		rq.Set, rq.Flag = r.PathValue("set"), r.PathValue("flag")
		var body flagservice.Event
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, `{"error":"malformed"}`, http.StatusBadRequest)
			return
		}
		rq.Event = &body
		c034JSON(w, flagservice.Result{FlagSet: rq.Set, Flag: rq.Flag, Variant: "c034_e1", Epoch: "c034-epoch"})
	})
	mux.HandleFunc("DELETE /{set}/{flag}", func(w http.ResponseWriter, r *http.Request) {
		rq := r.Context().Value(c034ReqKey{}).(*c034Request)
		rq.Set, rq.Flag = r.PathValue("set"), r.PathValue("flag")
		c034JSON(w, map[string]any{})
	})

	rec.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rq := &c034Request{Method: r.Method, EscapedPath: r.URL.EscapedPath(), Query: r.URL.Query()}
		sw := &c034StatusWriter{ResponseWriter: w, status: http.StatusOK}
		mux.ServeHTTP(sw, r.WithContext(context.WithValue(r.Context(), c034ReqKey{}, rq)))
		rq.Status = sw.status
		rec.mu.Lock()
		rec.reqs = append(rec.reqs, *rq)
		rec.mu.Unlock()
	}))
	t.Cleanup(rec.srv.Close)
	return rec
}

func (rec *c034Recorder) requests() []c034Request {
	rec.mu.Lock()
	defer rec.mu.Unlock()
	return append([]c034Request(nil), rec.reqs...)
}

type c034StatusWriter struct {
	http.ResponseWriter
	status int
}

func (s *c034StatusWriter) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func c034JSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

// c034Row drives one agent writer. Drives names the write call sites (guard keys,
// relpath:Recv.Func) the row exercises; the guard test fails when a call site has no row.
type c034Row struct {
	name   string
	drives []string
	run    func(t *testing.T, svc *flagservice.Client)
	// check validates one write request's (set, key, scope); "" = allowed.
	check func(r c034Request) string
}

// c034Smuggled are flag keys that try to reach another set through the path.
var c034Smuggled = []string{"enabled", "agent/enabled", "../agent/enabled", "..%2Fagent%2Fenabled"}

const c034GameID = "game_c034"

var c034WriterRows = []c034Row{
	{
		name:   "interventions",
		drives: []string{"actions/writer.go:Writer.put", "actions/writer.go:Writer.post"},
		run: func(t *testing.T, svc *flagservice.Client) {
			w := actions.NewWriter(svc, testLogger())
			for _, iv := range c034Interventions(t) {
				d := decision.Decision{Intervention: iv, GameID: c034GameID, TargetSerial: "00:06:f7:c0:34:01"}
				err := w.Apply(context.Background(), d)
				if c034NonWriting[iv] {
					continue
				}
				if err != nil {
					t.Errorf("intervention %q: neither written nor on the known non-writing list: %v", iv, err)
				}
			}
		},
		check: func(r c034Request) string {
			if r.Set != actions.FlagSetInterventions {
				return "interventions writer wrote set " + r.Set
			}
			if !c034InterventionKeys[r.Flag] {
				return "key " + r.Flag + " is not a flags.md §13.1 interventions key"
			}
			switch {
			case r.Method == http.MethodPut && r.Write != nil:
				s := r.Write.Scope
				if s.GameID != c034GameID || s.ExperimentID != "" || s.Arm != "" || s.GameKind != "" || s.FractionPercent != nil || r.Write.Persist {
					return fmt.Sprintf("intervention PUT not game-scoped: %+v persist=%v", s, r.Write.Persist)
				}
			case r.Method == http.MethodPost && r.Event != nil:
				if r.Event.Scope != (flagservice.Scope{GameID: c034GameID}) {
					return fmt.Sprintf("intervention event not game-scoped: %+v", r.Event.Scope)
				}
			default:
				return "unexpected intervention request"
			}
			return ""
		},
	},
	{
		name:   "rollout",
		drives: []string{"actions/rollout_writer.go:RolloutWriter.SetControllerCount"},
		run: func(t *testing.T, svc *flagservice.Client) {
			w := actions.NewRolloutWriter(svc, testLogger())
			variants := []string{actions.StageNoneVariant}
			for cur := actions.StageNoneValue; ; {
				v, next, ok := actions.NextStage(cur)
				if !ok {
					break
				}
				variants = append(variants, v)
				cur = next
			}
			for _, v := range variants {
				if err := w.SetControllerCount(v); err != nil {
					t.Errorf("stage %s: %v", v, err)
				}
			}
		},
		check: func(r c034Request) string {
			if r.Method != http.MethodPut || r.Write == nil {
				return "rollout writer sent a non-PUT write"
			}
			if r.Set != actions.FlagSetRollout || r.Flag != "current_controller_count" {
				return "rollout writer wrote " + r.Set + "/" + r.Flag + ", only rollout/current_controller_count is allowed"
			}
			if r.Write.Scope != (flagservice.Scope{}) || r.Write.Persist {
				return fmt.Sprintf("rollout write not a stack overlay: %+v", r.Write.Scope)
			}
			return ""
		},
	},
	{
		name:   "experiment",
		drives: []string{"experiment/writer.go:Writer.Apply", "experiment/writer.go:Writer.DryRun", "experiment/writer.go:Writer.Strip"},
		run: func(t *testing.T, svc *flagservice.Client) {
			ctx := context.Background()
			log := testLogger()
			// Production wiring (experiment_loop.go): a Gate over the Writer, and the
			// registry's TargetingWriter over both.
			writer := experiment.NewWriter(svc, log)
			targeting := experiment.NewGateTargetingWriter(experiment.NewGate(writer, nil, log), writer)
			p := experiment.Proposal{FlagKey: "sensitivity", ExperimentID: experiment.MintExperimentID(), ExperimentalValue: 3}
			if err := targeting.Apply(ctx, p); err != nil {
				t.Errorf("gated apply: %v", err)
			}
			if err := targeting.Reassert(ctx, p); err != nil { // the rendered flag lacks the id: re-written
				t.Errorf("reassert: %v", err)
			}
			if err := targeting.Strip(ctx, p.FlagKey, p.ExperimentID); err != nil {
				t.Errorf("strip: %v", err)
			}
			// The Writer behind the Gate, with keys that try to leave the game set.
			for _, key := range c034Smuggled {
				sp := experiment.Proposal{FlagKey: key, ExperimentID: experiment.MintExperimentID(), ExperimentalValue: true}
				_ = targeting.Apply(ctx, sp) // the Gate refuses a smuggled path; no write
				_, _ = writer.DryRun(ctx, sp)
				_, _ = writer.Apply(ctx, sp)
				_ = writer.Strip(ctx, key, sp.ExperimentID)
			}
		},
		check: func(r c034Request) string {
			if r.Set != experiment.GameFlagSet {
				return "experiment writer wrote set " + r.Set
			}
			switch {
			case r.Method == http.MethodPut && r.Write != nil:
				s := r.Write.Scope
				if s.ExperimentID == "" || s.Arm != experiment.ArmExperimental || s.GameKind != "" || s.GameID != "" || s.Serial != "" || s.FractionPercent != nil || r.Write.Persist {
					return fmt.Sprintf("experiment PUT not experiment+arm scoped: %+v persist=%v", s, r.Write.Persist)
				}
			case r.Method == http.MethodDelete:
				if r.Query.Get("experiment_id") == "" || r.Query.Get("arm") != experiment.ArmExperimental || r.Query.Has("game_kind") || r.Query.Has("game_id") {
					return "experiment DELETE not experiment+arm scoped: " + r.Query.Encode()
				}
			default:
				return "unexpected experiment request"
			}
			return ""
		},
	},
	{
		name:   "promotion",
		drives: []string{"promote/realdefault.go:serviceRealDefaultWriter.SetRealDefault", "promote/realdefault.go:serviceRealDefaultWriter.revert"},
		run: func(t *testing.T, svc *flagservice.Client) {
			ctx := context.Background()
			t.Setenv("FLAG_SERVICE_URL", svc.BaseURL())
			t.Setenv("AGENT_CODE_IMPROVEMENT_ENABLED", "true")
			t.Setenv("AGENT_AUTONOMOUS_ENABLED", "true")
			t.Setenv("AGENT_EXPERIMENT_DIR", t.TempDir()) // promote's snap store
			realDef, err := promote.NewRealDefaultWriterFromEnv(testLogger())
			if err != nil || realDef == nil {
				t.Fatalf("real-default writer not built: %v", err)
			}
			for _, key := range append([]string{"sensitivity"}, c034Smuggled...) {
				if err := realDef.SetRealDefault(ctx, key, 3); err != nil {
					t.Errorf("set real default %q: %v", key, err)
				}
				if err := realDef.RevertRealDefault(ctx, key); err != nil {
					t.Errorf("revert real default %q: %v", key, err)
				}
			}
			// The guarded watch revert (#2236): same set, key and scope as the plain one.
			var guarded promote.GuardedRealDefaultReverter = realDef
			if err := realDef.SetRealDefault(ctx, "sensitivity", 3); err != nil {
				t.Errorf("set real default: %v", err)
			}
			if err := guarded.RevertRealDefaultUnlessRealGame(ctx, "sensitivity"); err != nil {
				t.Errorf("guarded revert: %v", err)
			}
			// The kill switch (UC-043 A1, #1704): list the promotion overlays and revert them.
			pr := promote.NewPromoter(nil, nil, realDef, nil, realDef, promote.RepoRef{}, nil, testLogger())
			if got := pr.RevertOnKillSwitch(ctx); len(got) == 0 || got[0].Outcome != promote.OutcomeReverted {
				t.Errorf("kill switch reverted nothing: %+v", got)
			}
		},
		check: func(r c034Request) string {
			if r.Set != "game" {
				return "promotion wrote set " + r.Set
			}
			switch {
			case r.Method == http.MethodPut && r.Write != nil:
				if r.Write.Scope != (flagservice.Scope{GameKind: "real"}) || r.Write.Persist {
					return fmt.Sprintf("promotion PUT not a game_kind=real overlay: %+v", r.Write.Scope)
				}
			case r.Method == http.MethodDelete:
				if r.Query.Get("game_kind") != "real" || r.Query.Has("experiment_id") || r.Query.Has("game_id") {
					return "promotion DELETE not a game_kind=real revert: " + r.Query.Encode()
				}
			default:
				return "unexpected promotion request"
			}
			return ""
		},
	},
}

// c034NonWriting are the interventions that write nothing by design.
var c034NonWriting = map[string]bool{
	decision.InterventionNoop: true, // probe-mode synthetic decision: a dispatched success with no write
}

// TestC034_NoAgentWriterLoosensItsLimits drives every agent writer and checks
// each request on the wire (C-034, FR-152, #1799).
func TestC034_NoAgentWriterLoosensItsLimits(t *testing.T) {
	for _, row := range c034WriterRows {
		t.Run(row.name, func(t *testing.T) {
			rec := newC034Recorder(t)
			row.run(t, flagservice.New(rec.srv.URL))
			writes := 0
			for _, r := range rec.requests() {
				if msg := c034WireViolation(r); msg != "" {
					t.Errorf("C-034 VIOLATION (%s): %s", msg, r)
					continue
				}
				if r.Method == http.MethodGet {
					continue
				}
				writes++
				if msg := row.check(r); msg != "" {
					t.Errorf("C-034 VIOLATION (%s): %s", msg, r)
				}
			}
			if writes == 0 {
				t.Errorf("row %s sent no write: the driver no longer exercises its writer", row.name)
			}
		})
	}
}

// c034WireViolation are the checks every request must pass, whatever the writer.
func c034WireViolation(r c034Request) string {
	if r.Status/100 != 2 {
		return "non-2xx (redirect or unrouted request)"
	}
	segs := strings.Split(strings.TrimPrefix(r.EscapedPath, "/"), "/")
	if first, err := url.PathUnescape(segs[0]); err != nil || first == "agent" {
		return "request touches the agent set"
	}
	if r.Method == http.MethodGet {
		return ""
	}
	if len(segs) != 2 {
		return "write path is not /{set}/{flag}"
	}
	switch r.Set {
	case actions.FlagSetInterventions, actions.FlagSetRollout, experiment.GameFlagSet:
		return ""
	}
	return "write outside the interventions, rollout and game sets"
}

// c034Interventions enumerates every decision.Intervention* constant from the
// decision package's source, so a new intervention is driven automatically.
func c034Interventions(t *testing.T) []string {
	t.Helper()
	fset := token.NewFileSet()
	files, err := filepath.Glob("decision/*.go")
	if err != nil || len(files) == 0 {
		t.Fatalf("decision sources: %v", err)
	}
	seen := map[string]bool{}
	for _, file := range files {
		if strings.HasSuffix(file, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, file, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		for _, decl := range f.Decls {
			gd, ok := decl.(*ast.GenDecl)
			if !ok || gd.Tok != token.CONST {
				continue
			}
			for _, spec := range gd.Specs {
				vs := spec.(*ast.ValueSpec)
				for i, name := range vs.Names {
					if !strings.HasPrefix(name.Name, "Intervention") || i >= len(vs.Values) {
						continue
					}
					lit, ok := vs.Values[i].(*ast.BasicLit)
					if !ok || lit.Kind != token.STRING {
						t.Fatalf("%s is not a string literal constant", name.Name)
					}
					v, _ := strconv.Unquote(lit.Value)
					seen[v] = true
				}
			}
		}
	}
	out := make([]string, 0, len(seen))
	for v := range seen {
		out = append(out, v)
	}
	sort.Strings(out)
	if len(out) < 10 {
		t.Fatalf("found only %d interventions in decision/: %v", len(out), out)
	}
	return out
}

// c034InterventionKeys are the keys of the flags.md §13.1 table (the spec), read
// from docs/specs/flags.md.
var c034InterventionKeys = func() map[string]bool {
	raw, err := os.ReadFile("../../docs/specs/flags.md")
	if err != nil {
		return map[string]bool{}
	}
	doc := string(raw)
	start := strings.Index(doc, "### 13.1 ")
	if start < 0 {
		return map[string]bool{}
	}
	section := doc[start:]
	if end := strings.Index(section[4:], "\n### "); end >= 0 {
		section = section[:end+4]
	}
	keys := map[string]bool{}
	for _, m := range regexp.MustCompile("(?m)^\\| `([a-z_]+)` \\|").FindAllStringSubmatch(section, -1) {
		keys[m[1]] = true
	}
	return keys
}()

func TestC034_InterventionKeysReadFromSpec(t *testing.T) {
	if len(c034InterventionKeys) < 10 {
		t.Fatalf("flags.md §13.1 gave %d keys; the spec table moved or the path is wrong", len(c034InterventionKeys))
	}
}

// c034HumanOnlyGameKeys are game-set flags only a human may change (stage
// overlay): the charge expiry of #1837 (UC-040 BR-019) and the charge cue flags
// of #2094 (UC-040 BR-021). The agent writes the game set only as
// experiment+arm or game_kind=real overlays, so these keys are out of its reach
// exactly when their registry rows allow neither scope; the dynamic declarer
// then never offers them either.
var c034HumanOnlyGameKeys = []string{
	"shield.charge_expiry_seconds",
	"shield.charge_rumble_intensity", "shield.charge_rumble_pulse_ms",
	"shield.charge_reminder_seconds", "shield.charge_pre_expiry_seconds",
	"shield.ending_rumble_seconds",
	// The hold feedback flags of #2463 (UC-040 BR-026).
	"shield.hold_rumble_max_intensity", "shield.hold_led_floor_percent",
	// The shield end grace of #2464 (UC-040 BR-034): it lengthens every agent shield.
	"shield.end_grace_seconds",
}

// c034HoldKeyCeilings are the eight hold keys FR-162 makes agent-writable as
// experiment+arm and real values (UC-043 A17), with the registry range a human
// set as their ceiling (C-034). shield.activation has variants, not a range.
var c034HoldKeyCeilings = map[string][]float64{
	"shield.activation":             nil,
	"shield.energy_max_seconds":     {1, 10},
	"shield.energy_budget_seconds":  {3, 30},
	"shield.energy_initial_seconds": {0, 5},
	"shield.recharge_per_second":    {0, 0.5},
	"shield.min_burst_seconds":      {0.1, 1},
	"shield.hold_rumble_intensity":  {0, 150},
	"shield.empty_pulse_ms":         {100, 800},
}

// UC: UC-043 A17
func TestC034_HoldKeysCeilings(t *testing.T) {
	policies, err := fake.RegistryPolicies(filepath.Join("..", "flag-service", "registry.yaml"), experiment.GameFlagSet)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	var widened []string
	for key, p := range policies {
		if strings.HasPrefix(key, "shield.") && slices.Contains(p.Scopes, "experiment+arm") {
			widened = append(widened, key)
		}
	}
	sort.Strings(widened)
	want := make([]string, 0, len(c034HoldKeyCeilings))
	for key := range c034HoldKeyCeilings {
		want = append(want, key)
	}
	sort.Strings(want)
	if !slices.Equal(widened, want) {
		t.Fatalf("UC-043/A17 the agent-writable shield keys are %v, want exactly %v (C-034)", widened, want)
	}
	// Every one of them carries the flag service's real-write guard (409 real_game_live
	// while a real game is registered, #2216), so no real write lands mid-game.
	rawRegistry, err := os.ReadFile(filepath.Join("..", "flag-service", "registry.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Flags []struct {
			Set       string `yaml:"set"`
			Key       string `yaml:"key"`
			RealWrite string `yaml:"real_write"`
		} `yaml:"flags"`
	}
	if err := yaml.Unmarshal(rawRegistry, &doc); err != nil {
		t.Fatal(err)
	}
	guarded := map[string]bool{}
	for _, f := range doc.Flags {
		if f.Set == experiment.GameFlagSet && f.RealWrite == "no_live_real_game" {
			guarded[f.Key] = true
		}
	}
	for _, key := range want {
		if !guarded[key] {
			t.Errorf("UC-043/A17 %s has no real_write: no_live_real_game in registry.yaml", key)
		}
	}
	writes := func(value any) []flagservice.Write {
		return []flagservice.Write{
			{Value: value, Scope: flagservice.Scope{ExperimentID: "exp_c034", Arm: experiment.ArmExperimental}},
			{Value: value, Scope: flagservice.Scope{GameKind: "real"}},
		}
	}
	for key, ceiling := range c034HoldKeyCeilings {
		t.Run("UC-043/A17 "+key, func(t *testing.T) {
			p := policies[key]
			if !slices.Equal(p.Scopes, []string{"stack", "experiment+arm", "real"}) {
				t.Fatalf("%s scopes %v, want [stack experiment+arm real]", key, p.Scopes)
			}
			if ceiling == nil {
				return
			}
			if !slices.Equal(p.Range, ceiling) {
				t.Fatalf("C-034: %s range %v, want the ceiling %v", key, p.Range, ceiling)
			}
			svc := fake.New()
			if err := svc.LoadSetWithPolicies(experiment.GameFlagSet, "../flagd/game.json", policies); err != nil {
				t.Fatalf("load: %v", err)
			}
			above, below := ceiling[1]+ceiling[1]/10+0.1, ceiling[0]-0.1
			for _, w := range append(writes(above), writes(below)...) {
				if _, err := svc.Put(context.Background(), experiment.GameFlagSet, key, w); err == nil {
					t.Errorf("C-034 VIOLATION: %s accepted %v outside %v in scope %+v", key, w.Value, ceiling, w.Scope)
				}
			}
			for _, w := range append(writes(ceiling[1]), writes(ceiling[0])...) {
				if _, err := svc.Put(context.Background(), experiment.GameFlagSet, key, w); err != nil {
					t.Errorf("%s refused %v at the ceiling in scope %+v: %v", key, w.Value, w.Scope, err)
				}
			}
		})
	}
}

// UC: UC-040 BR-019
func TestC034_ShieldChargeFlagsAreNeverWrittenByTheAgent(t *testing.T) {
	policies, err := fake.RegistryPolicies(filepath.Join("..", "flag-service", "registry.yaml"), experiment.GameFlagSet)
	if err != nil {
		t.Fatalf("read registry: %v", err)
	}
	for _, shipped := range []string{"../flagd/game.json", "../flagd/ci/game.json"} {
		svc := fake.New()
		if err := svc.LoadSetWithPolicies(experiment.GameFlagSet, shipped, policies); err != nil {
			t.Fatalf("load %s: %v", shipped, err)
		}
		d := &dynamicDeclarer{gameFlags: svc, defaultObjective: decision.ObjectiveBalanced}
		candidates := d.readCandidateFlags()
		for _, key := range c034HumanOnlyGameKeys {
			t.Run("UC-040/BR-019 "+shipped+" "+key, func(t *testing.T) {
				view, err := svc.GetFlag(context.Background(), experiment.GameFlagSet, key)
				if err != nil {
					t.Fatalf("%s missing from %s: %v", key, shipped, err)
				}
				if view.Policy == nil || !view.Policy.AllowsScope("stack") {
					t.Fatalf("%s is not a stack overlay: %+v", key, view.Policy)
				}
				for _, scope := range []string{"experiment+arm", "real", "game"} {
					if view.Policy.AllowsScope(scope) {
						t.Errorf("C-034 VIOLATION: %s allows scope %s, which an agent writer can use", key, scope)
					}
				}
				if _, ok := candidates[key]; ok {
					t.Errorf("C-034 VIOLATION: %s is in the agent's experiment catalog", key)
				}
				// Every declared variant of the key, so only the scope can refuse the write.
				if len(view.Variants) == 0 {
					t.Fatalf("%s has no declared variants", key)
				}
				for _, variant := range view.Variants {
					experimentWrite := flagservice.Write{Variant: variant, Scope: flagservice.Scope{ExperimentID: "exp_c034", Arm: experiment.ArmExperimental}}
					realWrite := flagservice.Write{Variant: variant, Scope: flagservice.Scope{GameKind: "real"}}
					for _, w := range []flagservice.Write{experimentWrite, realWrite} {
						if _, err := svc.Put(context.Background(), experiment.GameFlagSet, key, w); err == nil {
							t.Errorf("C-034 VIOLATION: the flag service accepted an agent-shaped write of %s=%s: %+v", key, variant, w.Scope)
						}
					}
				}
			})
		}
	}
}
