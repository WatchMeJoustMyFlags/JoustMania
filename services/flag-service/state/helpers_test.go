package state

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/diegoholiveira/jsonlogic/v3"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/fserr"
	"github.com/joustmania/flag-service/registry"
)

// repoPath resolves a path relative to services/flag-service.
func repoPath(t *testing.T, rel string) string {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(file), "..", rel)
}

type fakeClock struct{ now time.Time }

func (c *fakeClock) Now() time.Time          { return c.now }
func (c *fakeClock) Advance(d time.Duration) { c.now = c.now.Add(d) }

type fixture struct {
	t     *testing.T
	state *State
	clock *fakeClock
	sub   *Subscription
}

type fixtureOpts struct {
	served    []string
	stateFile string
	epoch     string
	noStream  bool
	k         int
	maxGames  int
	// registryYAML replaces registry.yaml when set (a registry row the real
	// file does not have yet, e.g. a widened scope).
	registryYAML string
}

// newFixture builds a State over the real CI flag files and registry.yaml,
// serving every set unless told otherwise, with one open sync stream.
func newFixture(t *testing.T, o fixtureOpts) *fixture {
	t.Helper()
	base, err := baseline.Load(repoPath(t, "../flagd/ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatalf("baseline: %v", err)
	}
	var reg *registry.Registry
	if o.registryYAML != "" {
		reg, err = registry.Parse([]byte(o.registryYAML), base)
	} else {
		reg, err = registry.LoadFile(repoPath(t, "registry.yaml"), base)
	}
	if err != nil {
		t.Fatalf("registry: %v", err)
	}
	if o.served == nil {
		o.served = base.Order
	}
	if o.epoch == "" {
		o.epoch = "e1"
	}
	clock := &fakeClock{now: time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)}
	st, _, err := New(base, reg, Options{
		Epoch: o.epoch, Served: o.served, StateFile: o.stateFile, EventListLen: o.k, MaxGames: o.maxGames,
		Now: clock.Now, Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
	})
	if err != nil {
		t.Fatalf("state: %v", err)
	}
	f := &fixture{t: t, state: st, clock: clock}
	if !o.noStream {
		sub, err := st.Subscribe("")
		if err != nil {
			t.Fatal(err)
		}
		f.sub = sub
		t.Cleanup(sub.Close)
	}
	return f
}

func (f *fixture) register(id, instance string, serials ...string) {
	f.t.Helper()
	if _, err := f.state.RegisterGame(Game{ID: id, CoordinatorInstance: instance, Kind: "shadow", Serials: serials}); err != nil {
		f.t.Fatalf("register %s: %v", id, err)
	}
}

func (f *fixture) put(req WriteRequest) WriteResult {
	f.t.Helper()
	res, err := f.state.Put(req)
	if err != nil {
		f.t.Fatalf("put %s/%s: %v", req.Set, req.Flag, err)
	}
	return res
}

// snapshotFlag returns one flag of the full merged configuration.
func (f *fixture) snapshotFlag(key string) map[string]any {
	f.t.Helper()
	payload, _, err := f.state.Snapshot("")
	if err != nil {
		f.t.Fatal(err)
	}
	var cfg struct {
		Flags map[string]map[string]any `json:"flags"`
	}
	if err := json.Unmarshal(payload, &cfg); err != nil {
		f.t.Fatal(err)
	}
	flag, ok := cfg.Flags[key]
	if !ok {
		f.t.Fatalf("flag %s not in the snapshot", key)
	}
	return flag
}

// resolve evaluates a rendered flag the way flagd does for a context: run the
// targeting (JSONLogic); a null / missing result falls back to defaultVariant.
// It returns the variant and its value.
func (f *fixture) resolve(key string, ctx map[string]any) (string, any) {
	f.t.Helper()
	flag := f.snapshotFlag(key)
	variant, _ := flag["defaultVariant"].(string)
	if targeting, ok := flag["targeting"]; ok && targeting != nil {
		rule, _ := json.Marshal(targeting)
		data, _ := json.Marshal(ctx)
		var out bytes.Buffer
		if err := jsonlogic.Apply(bytes.NewReader(rule), bytes.NewReader(data), &out); err != nil {
			f.t.Fatalf("jsonlogic %s: %v", key, err)
		}
		var result any
		_ = json.Unmarshal(out.Bytes(), &result)
		if s, ok := result.(string); ok && s != "" {
			variant = s
		}
	}
	variants, _ := flag["variants"].(map[string]any)
	return variant, variants[variant]
}

func wantReason(t *testing.T, err *fserr.Error, status int, reason string) {
	t.Helper()
	if err == nil {
		t.Fatalf("want %d %s, got success", status, reason)
	}
	if err.Status != status || err.Reason != reason {
		t.Fatalf("want %d %s, got %d %s (%s)", status, reason, err.Status, err.Reason, err.Detail)
	}
}

func raw(s string) json.RawMessage { return json.RawMessage(s) }

func intp(n int) *int { return &n }

func ttl(seconds float64) *float64 { return &seconds }

func containsStr(haystack []byte, needle string) bool {
	return strings.Contains(string(haystack), needle)
}
