package main

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
)

// experiment_seed_test.go covers the experiment_seed object flag (#1907,
// flags.md §3): its schema, the last-value latch (UC-070 BR-007, UC-043 A1),
// the declarer lock and its place in the tick, and the shipped variants.

// lockedBuffer is a goroutine-safe log sink for counting agent log lines.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) count(needle string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return strings.Count(b.buf.String(), needle)
}

// seedLoop is a loop over a real registry and the declarer's fake game service,
// with both gates on, no dynamic declarer and a captured log. cfg is the live
// config the configOverride serves; seed is its experiment_seed (nil = unresolved).
type seedLoop struct {
	*experimentLoop
	logs *lockedBuffer
	mu   sync.Mutex
	cfg  flags.ExperimentConfig
}

func newSeedLoop(t *testing.T) *seedLoop {
	t.Helper()
	loop, _ := loopWithRealRegistry(t, 8)
	logs := &lockedBuffer{}
	loop.log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	sl := &seedLoop{experimentLoop: loop, logs: logs}
	sl.cfg = flags.ExperimentConfig{Enabled: true, Tick: time.Hour, ShadowEffectiveConcurrency: 4, DynamicMaxConcurrent: defaultDynamicMaxConcurrent}
	loop.configOverride = func(context.Context) flags.ExperimentConfig {
		sl.mu.Lock()
		defer sl.mu.Unlock()
		return sl.cfg
	}
	return sl
}

func (s *seedLoop) set(enabled bool, seed any) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cfg.Enabled = enabled
	s.cfg.Seed = seed
}

const (
	logSeedDeclared  = "experiment_seed declared an experiment"
	logSeedMalformed = "experiment_seed is malformed"
	logSeedFailed    = "seed declare/start failed"
	logSeedSatisfied = "experiment_seed satisfied by a live experiment"
)

// UC: UC-070 BR-007
// UC: UC-043 A1
func TestParseExperimentSeed(t *testing.T) {
	valid := func(extra map[string]any) map[string]any {
		m := map[string]any{"flag": "nonstop.respawn_seconds", "value": 4.5}
		for k, v := range extra {
			m[k] = v
		}
		return m
	}
	t.Run("UC-070/BR-007 an empty object is off", func(t *testing.T) {
		if _, kind, _ := parseExperimentSeed(map[string]any{}); kind != seedOff {
			t.Fatalf("kind = %v, want seedOff", kind)
		}
	})
	t.Run("UC-070/BR-007 a full seed parses to its intent", func(t *testing.T) {
		in, kind, reason := parseExperimentSeed(valid(map[string]any{
			"objective": "endurance", "target_n": 8.0, "hypothesis": " h ",
		}))
		if kind != seedValid {
			t.Fatalf("kind = %v (%s), want seedValid", kind, reason)
		}
		if in.FlagKey != "nonstop.respawn_seconds" || in.ExperimentalValue != 4.5 || in.Objective != "endurance" || in.TargetNPerArm != 8 || in.Hypothesis != "h" {
			t.Fatalf("intent = %+v", in)
		}
	})
	t.Run("UC-070/BR-007 optional fields take their defaults", func(t *testing.T) {
		in, kind, _ := parseExperimentSeed(valid(nil))
		if kind != seedValid || in.Objective != decision.ObjectiveBalanced || in.TargetNPerArm != 0 || in.Hypothesis != seedDefaultHypothesis {
			t.Fatalf("kind=%v intent=%+v", kind, in)
		}
	})
	t.Run("UC-070/BR-007 a string value stays a string (no re-parse)", func(t *testing.T) {
		in, kind, _ := parseExperimentSeed(map[string]any{"flag": "f", "value": "4.5"})
		if kind != seedValid || in.ExperimentalValue != "4.5" {
			t.Fatalf("kind=%v value=%#v, want the string 4.5", kind, in.ExperimentalValue)
		}
	})
	t.Run("UC-070/BR-007 an integer target_n from an in-memory provider is accepted", func(t *testing.T) {
		if in, kind, _ := parseExperimentSeed(valid(map[string]any{"target_n": 3})); kind != seedValid || in.TargetNPerArm != 3 {
			t.Fatalf("kind=%v target=%d", kind, in.TargetNPerArm)
		}
	})
	malformed := map[string]any{
		"not an object":         "windows",
		"a list":                []any{"windows"},
		"missing flag":          map[string]any{"value": 1.0},
		"empty flag":            map[string]any{"flag": " ", "value": 1.0},
		"flag not a string":     map[string]any{"flag": 3.0, "value": 1.0},
		"missing value":         map[string]any{"flag": "f"},
		"null value":            map[string]any{"flag": "f", "value": nil},
		"chaos objective":       valid(map[string]any{"objective": "chaos"}),
		"unknown objective":     valid(map[string]any{"objective": "fastest"}),
		"objective not string":  valid(map[string]any{"objective": 1.0}),
		"negative target_n":     valid(map[string]any{"target_n": -1.0}),
		"fractional target_n":   valid(map[string]any{"target_n": 2.5}),
		"string target_n":       valid(map[string]any{"target_n": "8"}),
		"hypothesis not string": valid(map[string]any{"hypothesis": 7.0}),
		"unknown field":         valid(map[string]any{"targetN": 8.0}),
	}
	for name, raw := range malformed {
		t.Run("UC-070/BR-007 malformed: "+name, func(t *testing.T) {
			if _, kind, reason := parseExperimentSeed(raw); kind != seedMalformed || reason == "" {
				t.Fatalf("kind = %v reason=%q, want seedMalformed with a reason", kind, reason)
			}
		})
	}
}

// UC: UC-070 BR-007
// UC: UC-043 A1
func TestMaybeDeclareSeed_Latch(t *testing.T) {
	ctx := context.Background()
	seedA := testSeed("nonstop.respawn_seconds", 6.0, 50)
	seedB := testSeed("nonstop.respawn_seconds", 5.0, 50)

	t.Run("UC-070/BR-007 a seed value is declared once, not again on the next tick", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		s.allocateIfEnabled(ctx)
		if _, ok := s.registry.ActiveFlags()["nonstop.respawn_seconds"]; !ok || s.registry.LiveCount() != 1 {
			t.Fatalf("active = %v, want the one seed experiment", s.registry.ActiveFlags())
		}
		if n := s.logs.count(logSeedDeclared); n != 1 {
			t.Fatalf("seed declared %d times, want 1", n)
		}
	})

	t.Run("UC-043/A1 a re-enable with the seed left on does not re-declare it", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		s.set(false, seedA) // experiments off: the seed experiment is aborted
		s.allocateIfEnabled(ctx)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 0 || s.logs.count(logSeedDeclared) != 1 {
			t.Fatalf("LiveCount = %d, declared %d; want 0 and 1: the seed left on stays latched",
				got, s.logs.count(logSeedDeclared))
		}
	})

	t.Run("UC-043/A1 off seen while the gates are off re-arms the same value", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		s.set(false, seedA) // agent-dryrun-enable.sh off: gates first...
		s.allocateIfEnabled(ctx)
		s.set(false, map[string]any{}) // ...then the seed, seen on a disabled tick
		s.allocateIfEnabled(ctx)
		s.set(true, seedA) // agent-dryrun-enable.sh on
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 1 || s.logs.count(logSeedDeclared) != 2 {
			t.Fatalf("LiveCount = %d, declared %d; want 1 and 2: off -> on declares the seed again",
				got, s.logs.count(logSeedDeclared))
		}
	})

	t.Run("UC-070/BR-007 off -> on while the seed experiment is still live does not stack", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		s.set(true, map[string]any{}) // seed off, the experiment keeps running
		s.allocateIfEnabled(ctx)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 1 || s.logs.count(logSeedSatisfied) != 1 {
			t.Fatalf("LiveCount = %d, satisfied %d; want 1 and 1: the re-armed seed is not stacked",
				got, s.logs.count(logSeedSatisfied))
		}
	})

	t.Run("UC-070/BR-007 A -> B -> A declares A again", func(t *testing.T) {
		s := newSeedLoop(t)
		for _, seed := range []map[string]any{seedA, seedB, seedA} {
			s.set(true, seed)
			s.allocateIfEnabled(ctx)
			s.set(false, seed) // end the experiment so the next value is not deduped
			s.allocateIfEnabled(ctx)
		}
		if n := s.logs.count(logSeedDeclared); n != 3 {
			t.Fatalf("A -> B -> A declared %d times, want 3: the latch is the last value only", n)
		}
	})

	t.Run("UC-070/BR-007 an eval error does not re-arm the seed", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		s.set(false, nil) // gates off and the flag unreadable
		s.allocateIfEnabled(ctx)
		s.set(true, nil)
		s.allocateIfEnabled(ctx)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 0 || s.logs.count(logSeedDeclared) != 1 {
			t.Fatalf("LiveCount = %d, declared %d; want 0 and 1: an eval error never clears the latch",
				got, s.logs.count(logSeedDeclared))
		}
	})

	t.Run("UC-070/BR-007 an unresolved flag declares nothing and latches nothing", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, nil)
		s.allocateIfEnabled(ctx)
		if s.seedLatched || s.logs.count(logSeedMalformed) != 0 || s.registry.LiveCount() != 0 {
			t.Fatalf("eval error latched %v / warned %d / declared %d; want nothing",
				s.seedLatched, s.logs.count(logSeedMalformed), s.registry.LiveCount())
		}
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 1 {
			t.Fatalf("LiveCount = %d, want 1: the seed is declared once the flag resolves", got)
		}
	})

	t.Run("UC-070/BR-007 a seed whose flag has a live experiment is skipped and latched (no stacking)", func(t *testing.T) {
		s := newSeedLoop(t)
		other := seedB
		in, _, _ := parseExperimentSeed(other)
		if _, err := s.declareAndStart(ctx, in); err != nil {
			t.Fatalf("declare the live experiment: %v", err)
		}
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 1 || s.logs.count(logSeedSatisfied) != 1 {
			t.Fatalf("LiveCount = %d, satisfied logs = %d; want 1 and 1 (seed not stacked)", got, s.logs.count(logSeedSatisfied))
		}
		s.set(false, seedA) // the live experiment ends
		s.allocateIfEnabled(ctx)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		if got := s.registry.LiveCount(); got != 0 {
			t.Fatalf("LiveCount = %d, want 0: the skipped seed value stays latched", got)
		}
	})

	t.Run("UC-043/A8 a refused declare is latched and logged once", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, testSeed("not_an_experiment_flag", 1.0, 3))
		for range 3 {
			s.allocateIfEnabled(ctx)
		}
		if n := s.logs.count(logSeedFailed); n != 1 {
			t.Fatalf("refused seed logged %d errors over 3 ticks, want 1", n)
		}
		if got := s.registry.LiveCount(); got != 0 {
			t.Fatalf("LiveCount = %d, want 0", got)
		}
	})

	t.Run("UC-070/BR-007 a malformed value declares nothing and warns once per value", func(t *testing.T) {
		s := newSeedLoop(t)
		values := []map[string]any{}
		for _, extra := range []map[string]any{
			{"objective": "chaos"}, {"objective": "fastest"}, {"target_n": -3.0},
			{"target_n": 2.5}, {"target_n": "8"}, {"hypothesis": 1.0}, {"extra": true},
		} {
			v := testSeed("nonstop.respawn_seconds", 6.0, 50)
			for k, x := range extra {
				v[k] = x
			}
			values = append(values, v)
		}
		for _, v := range values {
			s.set(true, v)
			s.allocateIfEnabled(ctx)
			s.allocateIfEnabled(ctx)
		}
		if got := s.registry.LiveCount(); got != 0 {
			t.Fatalf("LiveCount = %d, want 0: a malformed seed is never half-applied", got)
		}
		if n := s.logs.count(logSeedMalformed); n != len(values) {
			t.Fatalf("WARN count = %d, want one per distinct value (%d)", n, len(values))
		}
	})
}

// The seed is declared before the dynamic declarer, so with a budget of one it
// takes the slot and the declarer declares nothing that tick.
// UC: UC-070 BR-007
func TestMaybeDeclareSeed_WinsTheBudgetSlot(t *testing.T) {
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	cfg := dynCfg(1)
	cfg.DynamicCandidates = "sensitivity"
	cfg.ProposerEnabled = false
	cfg.Seed = testSeed("nonstop.respawn_seconds", 6.0, 50)
	loop.configOverride = func(context.Context) flags.ExperimentConfig { return cfg }

	loop.allocateIfEnabled(context.Background())

	got := loop.registry.ActiveFlags()
	want := map[string]struct{}{"nonstop.respawn_seconds": {}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("UC-070/BR-007 active flags = %v, want only the seed's %v (seed first, budget 1)", got, want)
	}
}

// Concurrent allocateIfEnabled callers (tick and game-end nudges) declare the
// seed exactly once: the latch check, dedup and Declare are one critical section
// under the declarer lock.
// UC: UC-070 BR-007
func TestMaybeDeclareSeed_ConcurrentCallersDeclareOnce(t *testing.T) {
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	logs := &lockedBuffer{}
	loop.log = slog.New(slog.NewTextHandler(logs, nil))
	cfg := flags.ExperimentConfig{Enabled: true, Tick: time.Hour, ShadowEffectiveConcurrency: 4,
		Seed: testSeed("nonstop.respawn_seconds", 6.0, 50)}
	loop.configOverride = func(context.Context) flags.ExperimentConfig { return cfg }

	start := make(chan struct{})
	var wg sync.WaitGroup
	for range 16 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			loop.allocateIfEnabled(context.Background())
		}()
	}
	close(start)
	wg.Wait()

	if n := logs.count(logSeedDeclared); n != 1 || loop.registry.LiveCount() != 1 {
		t.Fatalf("UC-070/BR-007 16 concurrent ticks declared the seed %d times (LiveCount %d), want exactly 1",
			n, loop.registry.LiveCount())
	}
}

// Flags.Experiment carries the raw experiment_seed object, and nil when flagd
// cannot answer (no seed, nothing latched).
// UC: UC-070 BR-007
func TestExperimentConfig_SeedFromFlag(t *testing.T) {
	seed := testSeed("windows", map[string]any{"a": 1.0}, 8)
	ev := &switchableEvaluator{objects: map[string]any{"experiment_seed": seed}}
	f := flags.New(ev, nil)
	if got := f.Experiment(context.Background(), experimentDefaults()).Seed; !reflect.DeepEqual(got, seed) {
		t.Fatalf("UC-070/BR-007 Seed = %#v, want the flag's object", got)
	}
	ev.setUnreachable()
	if got := f.Experiment(context.Background(), experimentDefaults()).Seed; got != nil {
		t.Fatalf("UC-070/BR-007 Seed with flagd unreachable = %#v, want nil", got)
	}
	if got := unresolvedConfig(experimentDefaults()).Seed; got != nil {
		t.Fatalf("UC-070/BR-007 unresolved config Seed = %#v, want nil (no seed)", got)
	}
}

// Every shipped experiment_seed variant parses under the flags.md §3 schema, both
// flag files carry the same flag, and the default variant `off` is {} (no seed,
// the compiled behaviour when the flag cannot be resolved). The flag-service half
// (each variant's value against its game key's bounds) is TestExperimentSeedVariants.
// UC: UC-070 BR-007
func TestExperimentSeedFlagVariantsParse(t *testing.T) {
	var seen json.RawMessage
	for _, path := range []string{filepath.Join("..", "flagd", "agent.json"), filepath.Join("..", "flagd", "ci", "agent.json")} {
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		var doc struct {
			Flags map[string]json.RawMessage `json:"flags"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		flagRaw, ok := doc.Flags["experiment_seed"]
		if !ok {
			t.Fatalf("%s has no experiment_seed flag", path)
		}
		if seen != nil && !jsonEqualRaw(t, seen, flagRaw) {
			t.Fatalf("UC-070/BR-007 experiment_seed differs between the flag files (%s)", path)
		}
		seen = flagRaw
		var flag struct {
			Variants       map[string]any `json:"variants"`
			DefaultVariant string         `json:"defaultVariant"`
		}
		if err := json.Unmarshal(flagRaw, &flag); err != nil {
			t.Fatal(err)
		}
		if flag.DefaultVariant != "off" {
			t.Fatalf("UC-070/BR-007 %s default variant = %q, want off", path, flag.DefaultVariant)
		}
		for name, v := range flag.Variants {
			_, kind, reason := parseExperimentSeed(v)
			want := seedValid
			if name == "off" {
				want = seedOff
			}
			if kind != want {
				t.Errorf("UC-070/BR-007 %s variant %s: kind %v (%s), want %v", path, name, kind, reason, want)
			}
		}
	}
}

func jsonEqualRaw(t *testing.T, a, b json.RawMessage) bool {
	t.Helper()
	var x, y any
	if json.Unmarshal(a, &x) != nil || json.Unmarshal(b, &y) != nil {
		t.Fatal("bad JSON")
	}
	return reflect.DeepEqual(x, y)
}

// A caller of allocateIfEnabled holding a stale config (tick and game-end nudges
// each read their own) never acts on an outdated experiment_seed: the seed
// decision re-reads the flag under the declarer lock (#1966). The stale caller is
// replayed deterministically by calling the seed steps with an old config after
// a newer tick has run.
// UC: UC-070 BR-007
// UC: UC-043 A1
func TestMaybeDeclareSeed_StaleConfigCaller(t *testing.T) {
	ctx := context.Background()
	seedA := testSeed("nonstop.respawn_seconds", 6.0, 50)
	off := map[string]any{}

	t.Run("UC-070/BR-007 a stale seed after off is observed is not declared again", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, seedA)
		s.allocateIfEnabled(ctx)
		stale := s.config(ctx) // a nudge that read seed A before the operator set off
		s.registry.AbortAll(ctx, "test: A's experiment ended")
		s.set(true, off)
		s.allocateIfEnabled(ctx) // observes off, clears the latch
		s.maybeDeclareSeed(ctx, stale)
		if n := s.logs.count(logSeedDeclared); n != 1 || s.registry.LiveCount() != 0 {
			t.Fatalf("declared %d times (LiveCount %d), want 1 and 0: a stale seed re-declared after off",
				n, s.registry.LiveCount())
		}
	})

	t.Run("UC-043/A1 a stale off does not re-arm a newer seed", func(t *testing.T) {
		s := newSeedLoop(t)
		s.set(true, off)
		stale := s.config(ctx) // a nudge that read off before the operator set A
		s.set(true, seedA)
		s.allocateIfEnabled(ctx) // declares A, latches it
		s.observeSeedOff(ctx, stale)
		s.registry.AbortAll(ctx, "test: A's experiment ended")
		s.allocateIfEnabled(ctx)
		if n := s.logs.count(logSeedDeclared); n != 1 {
			t.Fatalf("seed declared %d times, want 1: a stale off cleared the latch", n)
		}
	})
}
