package state

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"math/rand/v2"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
)

// The op-sequence property test of Put / Clear / ClearWriter / ExpireOverlays
// and the game registry (#2370). Random sequences, from fixed seeds so a plain
// `go test` is deterministic, are checked after every step against these
// invariants and a small model of the persisted (admin) layer:
//
//   - the snapshot version never goes down;
//   - a refusal, and an accepted dry run, change nothing: version, every slot,
//     the merged snapshot and the state file stay as they were;
//   - an accepted Put bumps the version and its slot holds the written value;
//     an accepted Clear bumps the version iff it cleared something;
//   - the admin layer equals the model (only an accepted persisted Put or
//     Clear moves it; a writer reset and a TTL expiry never do), and a fresh
//     State started from the state file restores exactly the model.
//
// The precondition matrix (precondition_matrix_test.go) pins each answer; this
// test pins what the answers do to the state over many steps.

// propSeeds are the fixed seeds a normal run checks.
var propSeeds = []uint64{1, 2, 3, 2370, 20261103}

const propSteps = 250

// propFlag is one flag the generator writes.
type propFlag struct {
	set, key string
	variants []string // declared variant names, one may be out of bounds
	values   []string // raw JSON values, in and out of bounds
	scopes   []Scope  // scopes the registry allows for the flag
}

var (
	stackSc = Scope{}
	realPSc = Scope{GameKind: "real"}
	exp1Sc  = Scope{ExperimentID: "e1", Arm: "a"}
	exp2Sc  = Scope{ExperimentID: "e2", Arm: "b"}
)

var propFlags = []propFlag{
	{"game", "sensitivity", []string{"slow", "medium", "fast", "nope"}, []string{"3", "1", "7", `"x"`}, []Scope{stackSc, realPSc, exp1Sc, exp2Sc}},
	{"game", "num_teams", []string{"two", "four", "seven"}, []string{"4", "9"}, []Scope{stackSc}},
	{"game", "shield.energy_budget_seconds", nil, []string{"5", "20", "31"}, []Scope{stackSc, realPSc, exp1Sc}},
	{"game", "death_grace_period_seconds", nil, []string{"0", "2.5", "6"}, []Scope{realPSc, exp1Sc, exp2Sc}},
	{"agent", "interventions_allowed", []string{"none", "probe", "ambient", "bogus"}, []string{`""`, `"noop"`}, []Scope{stackSc}},
	{"agent", "decision.openai.model", []string{"unset"}, []string{`"gpt-x"`, `"gpt-y"`, "3"}, []Scope{stackSc}},
}

var (
	propWriters = []string{"menu", "agent", "ops", ""}
	propScopes  = []Scope{
		{},
		{FractionPercent: intp(50)},
		{GameKind: "real"},
		{GameKind: "shadow"},
		{ExperimentID: "e1", Arm: "a"},
		{ExperimentID: "e2", Arm: "b"},
		{GameID: "g-shadow"},
		{Serial: "S1"},
	}
)

// adminEntry is the model of one persisted admin value.
type adminEntry struct {
	Value  string
	Writer string
}

type propRun struct {
	t     *testing.T
	rng   *rand.Rand
	base  *baseline.Baseline
	reg   *registry.Registry
	clock *fakeClock
	path  string
	st    *State
	model map[string]adminEntry // flag key -> persisted admin value
	// seen counts the outcomes, so a generator drift that only produces
	// refusals fails instead of passing vacuously.
	seen map[string]int
}

// UC: UC-053 main
func TestOpSequenceProperties(t *testing.T) {
	base, err := baseline.Load(repoPath(t, "../flagd/ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatalf("baseline: %v", err)
	}
	reg, err := registry.LoadFile(repoPath(t, "registry.yaml"), base)
	if err != nil {
		t.Fatalf("registry: %v", err)
	}
	for _, seed := range propSeeds {
		t.Run(fmt.Sprintf("UC-053/main seed=%d", seed), func(t *testing.T) {
			r := &propRun{
				t: t, rng: rand.New(rand.NewPCG(seed, seed^0x2370)), base: base, reg: reg,
				clock: &fakeClock{now: time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)},
				path:  filepath.Join(t.TempDir(), "state.json"),
				model: map[string]adminEntry{},
				seen:  map[string]int{},
			}
			r.st = r.open()
			sub, err := r.st.Subscribe("")
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(sub.Close)
			for step := 0; step < propSteps; step++ {
				r.step(step)
			}
			t.Logf("outcomes %v", r.seen)
			for _, outcome := range []string{"put", "put-persist", "put-dry", "clear", "clear-persist", "refused"} {
				if r.seen[outcome] == 0 {
					t.Errorf("no %s outcome in %d steps (seen %v): the generator no longer covers it", outcome, propSteps, r.seen)
				}
			}
		})
	}
}

// open starts a State over the run's state file.
func (r *propRun) open() *State {
	r.t.Helper()
	st, report, err := New(r.base, r.reg, Options{
		Epoch: "e1", Served: r.base.Order, StateFile: r.path,
		Now: r.clock.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		r.t.Fatalf("state: %v", err)
	}
	if report.Corrupt || len(report.Dropped) > 0 || report.FutureVersion {
		r.t.Fatalf("state file did not load cleanly: %+v", report)
	}
	return st
}

// observed is everything a step may change, captured before and after it.
type observed struct {
	version  uint64
	slots    string
	snapshot []byte
	file     []byte
}

func (r *propRun) observe() observed {
	r.t.Helper()
	snap, version, err := r.st.Snapshot("")
	if err != nil {
		r.t.Fatal(err)
	}
	file, err := os.ReadFile(r.path)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		r.t.Fatal(err)
	}
	return observed{version: version, slots: dumpSlots(r.st), snapshot: snap, file: file}
}

func (r *propRun) step(step int) {
	t := r.t
	t.Helper()
	before := r.observe()
	desc, refused, dry, wantBump := r.apply()
	after := r.observe()
	at := fmt.Sprintf("step %d: %s", step, desc)

	switch {
	case refused:
		r.seen["refused"]++
	case dry:
		r.seen["put-dry"]++
	}
	if after.version < before.version {
		t.Fatalf("%s: version went down %d -> %d", at, before.version, after.version)
	}
	if refused || dry {
		if after.version != before.version || after.slots != before.slots ||
			!bytes.Equal(after.snapshot, before.snapshot) || !bytes.Equal(after.file, before.file) {
			t.Fatalf("%s: a refusal or dry run changed the state (version %d -> %d)\nbefore %s\nafter  %s",
				at, before.version, after.version, before.slots, after.slots)
		}
	}
	switch wantBump {
	case bumpYes:
		if after.version <= before.version {
			t.Fatalf("%s: accepted change did not bump the version (%d)", at, after.version)
		}
	case bumpNo:
		if after.version != before.version {
			t.Fatalf("%s: a no-op moved the version %d -> %d", at, before.version, after.version)
		}
	}
	r.checkAdmin(at, r.st, "live")
	r.checkAdmin(at, r.open(), "restarted")
}

type bump int

const (
	bumpAny bump = iota
	bumpYes
	bumpNo
)

// apply runs one random op and updates the model. It returns a description,
// whether the op was refused, whether it was a dry run and what it must do to
// the version.
func (r *propRun) apply() (desc string, refused, dry bool, want bump) {
	switch n := r.rng.IntN(100); {
	case n < 50:
		return r.applyPut()
	case n < 80:
		return r.applyClear()
	case n < 85:
		set := []string{"game", "agent", "nope"}[r.rng.IntN(3)]
		w := pick(r.rng, propWriters)
		n, err := r.st.ClearWriter(set, w)
		if err != nil {
			return fmt.Sprintf("ClearWriter(%s,%q)", set, w), true, false, bumpNo
		}
		return fmt.Sprintf("ClearWriter(%s,%q)=%d", set, w, n), false, false, bumpFor(n > 0)
	case n < 92:
		r.clock.Advance(time.Duration(r.rng.IntN(40)) * time.Second)
		n := r.st.ExpireOverlays()
		return fmt.Sprintf("ExpireOverlays=%d", n), false, false, bumpFor(n > 0)
	case n < 96:
		id, kind := pick(r.rng, []string{"g-real", "g-shadow"}), "shadow"
		if id == "g-real" {
			kind = "real"
		}
		if _, err := r.st.RegisterGame(Game{ID: id, CoordinatorInstance: "c1", Kind: kind, Serials: []string{"S1"}}); err != nil {
			return "RegisterGame " + id, true, false, bumpNo
		}
		return "RegisterGame " + id, false, false, bumpAny
	default:
		id := pick(r.rng, []string{"g-real", "g-shadow"})
		r.st.EndGame(id, "test")
		return "EndGame " + id, false, false, bumpAny
	}
}

func bumpFor(changed bool) bump {
	if changed {
		return bumpYes
	}
	return bumpNo
}

func pick[T any](rng *rand.Rand, list []T) T { return list[rng.IntN(len(list))] }

func (r *propRun) applyPut() (string, bool, bool, bump) {
	pf := pick(r.rng, propFlags)
	// Mostly well-formed requests (so writes land), sometimes chaos (any
	// scope, any combination of options, mostly refusals).
	chaos := r.rng.IntN(10) < 2
	req := WriteRequest{Set: pf.set, Flag: pf.key, Writer: pick(r.rng, propWriters), Scope: pick(r.rng, pf.scopes)}
	if chaos {
		req.Scope = pick(r.rng, propScopes)
	}
	switch k := r.rng.IntN(10); {
	case k < 4 && len(pf.variants) > 0:
		req.Variant = pick(r.rng, pf.variants)
	case k < 9 || !chaos:
		req.Value = json.RawMessage(pick(r.rng, pf.values))
	default: // neither (a 400)
	}
	stack := req.Scope.Name() == registry.ScopeStack
	if r.rng.IntN(10) < 4 && (chaos || stack) {
		req.Persist = true
	}
	if r.rng.IntN(10) < 2 && (chaos || !req.Persist) {
		req.TTLSeconds = ttl(float64(1 + r.rng.IntN(30)))
	}
	if r.rng.IntN(10) < 2 && (chaos || !req.Persist) {
		req.DryRun = true
	}
	if r.rng.IntN(10) < 2 && (chaos || req.Scope.Name() == registry.ScopeReal) {
		req.IfWriterIn = []string{pick(r.rng, propWriters)}
	}
	desc := fmt.Sprintf("Put %s/%s variant=%q value=%s scope=%+v persist=%v ttl=%v dry=%v if_writer_in=%q",
		req.Set, req.Flag, req.Variant, req.Value, req.Scope, req.Persist, req.TTLSeconds != nil, req.DryRun, req.IfWriterIn)
	res, err := r.st.Put(req)
	if err != nil {
		return desc + " -> " + err.Reason, true, false, bumpNo
	}
	if req.DryRun {
		return desc + " -> dry", false, true, bumpNo
	}
	if req.Persist {
		if !res.Persisted {
			r.t.Fatalf("%s: a persisted Put with a writable state file answered persisted:false", desc)
		}
		r.model[req.Flag] = adminEntry{Value: string(res.Value), Writer: req.Writer}
		r.seen["put-persist"]++
	}
	r.seen["put"]++
	if got := slotValue(r.st, req.Flag, req.Scope, req.Persist); got == nil || !jsonEq(got, res.Value) {
		r.t.Fatalf("%s: slot holds %s, want %s", desc, got, res.Value)
	}
	return desc + " -> ok", false, false, bumpYes
}

func (r *propRun) applyClear() (string, bool, bool, bump) {
	pf := pick(r.rng, propFlags)
	chaos := r.rng.IntN(10) < 2
	req := ClearRequest{Set: pf.set, Flag: pf.key, Writer: pick(r.rng, propWriters), Scope: pick(r.rng, pf.scopes)}
	if chaos {
		req.Scope = pick(r.rng, propScopes)
	}
	if r.rng.IntN(10) < 5 && (chaos || req.Scope.Name() == registry.ScopeStack) {
		req.Persist = true
	}
	if r.rng.IntN(10) < 3 && (chaos || req.Scope.Name() == registry.ScopeReal) {
		req.IfWriter = pick(r.rng, propWriters[:3])
	}
	if r.rng.IntN(10) < 2 {
		req.UnlessRealGame = true
	}
	desc := fmt.Sprintf("Clear %s/%s scope=%+v persist=%v if_writer=%q unless_real_game=%v",
		req.Set, req.Flag, req.Scope, req.Persist, req.IfWriter, req.UnlessRealGame)
	res, err := r.st.Clear(req)
	if err != nil {
		return desc + " -> " + err.Reason, true, false, bumpNo
	}
	if req.Persist && res.Cleared > 0 {
		delete(r.model, req.Flag)
		r.seen["clear-persist"]++
	}
	if res.Cleared > 0 {
		r.seen["clear"]++
	}
	if got := slotValue(r.st, req.Flag, req.Scope, req.Persist); got != nil {
		r.t.Fatalf("%s: slot still holds %s after an accepted clear", desc, got)
	}
	return fmt.Sprintf("%s -> cleared %d", desc, res.Cleared), false, false, bumpFor(res.Cleared > 0)
}

// slotValue returns the JSON value in the slot a scope addresses (persist:
// the admin value), or nil when the slot is empty.
func slotValue(st *State, key string, sc Scope, persist bool) json.RawMessage {
	st.mu.Lock()
	defer st.mu.Unlock()
	f := st.flags[key]
	var v *value
	switch sc.Name() {
	case registry.ScopeStack:
		v = f.stack
		if persist {
			v = f.admin
		}
	case registry.ScopeReal:
		v = f.real
	case registry.ScopeFraction:
		v = f.fraction
	case registry.ScopeGame:
		v = f.games[sc.GameID]
	case registry.ScopeGameSerial:
		v = f.serials[sc.GameID][sc.Serial]
	case registry.ScopeExperiment:
		v = f.experiments[sc.ExperimentID][sc.Arm]
	}
	if v == nil {
		return nil
	}
	return f.jsonValue(v)
}

// checkAdmin compares a State's admin layer with the model.
func (r *propRun) checkAdmin(at string, st *State, which string) {
	r.t.Helper()
	st.mu.Lock()
	defer st.mu.Unlock()
	got := map[string]adminEntry{}
	for key, f := range st.flags {
		if f.admin == nil {
			continue
		}
		if !f.admin.Persisted {
			r.t.Fatalf("%s: %s admin value of %s is not marked persisted", at, which, key)
		}
		got[key] = adminEntry{Value: compactJSON(f.jsonValue(f.admin)), Writer: f.admin.Writer}
	}
	want := map[string]adminEntry{}
	for key, e := range r.model {
		want[key] = adminEntry{Value: compactJSON(json.RawMessage(e.Value)), Writer: e.Writer}
	}
	if fmt.Sprint(sortedEntries(got)) != fmt.Sprint(sortedEntries(want)) {
		r.t.Fatalf("%s: %s admin layer %v, model %v", at, which, sortedEntries(got), sortedEntries(want))
	}
}

func sortedEntries(m map[string]adminEntry) []string {
	out := make([]string, 0, len(m))
	for k, e := range m {
		out = append(out, fmt.Sprintf("%s=%s(%s)", k, e.Value, e.Writer))
	}
	sort.Strings(out)
	return out
}

func compactJSON(raw json.RawMessage) string {
	var b bytes.Buffer
	if err := json.Compact(&b, raw); err != nil {
		return string(raw)
	}
	return b.String()
}

func jsonEq(a, b json.RawMessage) bool { return compactJSON(a) == compactJSON(b) }

// dumpSlots renders every slot of every flag, deterministically.
func dumpSlots(st *State) string {
	st.mu.Lock()
	defer st.mu.Unlock()
	one := func(f *flagState, v *value) string {
		if v == nil {
			return "-"
		}
		return fmt.Sprintf("%s|%s|%s|%v|%v|%d", v.Variant, compactJSON(f.jsonValue(v)), v.Writer, v.ExpiresAt.Unix(), v.Persisted, v.Fraction)
	}
	var b bytes.Buffer
	for _, key := range sortedKeys(st.flags) {
		f := st.flags[key]
		fmt.Fprintf(&b, "%s{s=%s a=%s r=%s f=%s", key, one(f, f.stack), one(f, f.admin), one(f, f.real), one(f, f.fraction))
		for _, g := range sortedKeys(f.games) {
			fmt.Fprintf(&b, " g[%s]=%s", g, one(f, f.games[g]))
		}
		for _, g := range sortedKeys(f.serials) {
			for _, s := range sortedKeys(f.serials[g]) {
				fmt.Fprintf(&b, " gs[%s/%s]=%s", g, s, one(f, f.serials[g][s]))
			}
		}
		for _, e := range sortedKeys(f.experiments) {
			for _, a := range sortedKeys(f.experiments[e]) {
				fmt.Fprintf(&b, " x[%s/%s]=%s", e, a, one(f, f.experiments[e][a]))
			}
		}
		for _, g := range sortedKeys(f.events) {
			l := f.events[g]
			fmt.Fprintf(&b, " ev[%s]=%s/%d/%d", g, l.Variant, l.Seq, len(l.Events))
		}
		b.WriteString("}\n")
	}
	return b.String()
}
