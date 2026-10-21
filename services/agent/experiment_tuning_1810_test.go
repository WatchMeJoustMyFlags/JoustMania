package main

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/llm"
)

// experiment_tuning_1810_test.go covers #1810 PR A: the widening experiment
// settings are capped by the operator's env ceilings (C-034, FR-152 d-f), the
// proposer gate is env AND flag for both proposers, and the completed-game grace
// is a tuning flag read at use time (FR-150, FR-151).

// The concurrent-experiment budget is min(env ceiling, flag), a non-positive flag
// falls back to the compiled 3 (capped by the env), an env value <= 0 or invalid
// is no ceiling, and the shadow capacity clamps the result.
// UC: UC-042 A11 A3
func TestConcurrentBudget_EnvCeilingTable(t *testing.T) {
	cases := []struct {
		name     string
		env      string // AGENT_EXPERIMENT_DYNAMIC_MAX_CONCURRENT; "" = unset
		flag     int
		capacity int
		want     int
	}{
		{"UC-042/A11 env unset: the flag governs", "", 6, 8, 6},
		{"UC-042/A11 env caps a wider flag", "2", 6, 8, 2},
		{"UC-042/A11 a narrower flag tightens the env", "6", 1, 8, 1},
		{"UC-042/A11 flag 0 falls back to the compiled 3", "", 0, 8, 3},
		{"UC-042/A11 flag 0 fallback is capped by the env", "2", 0, 8, 2},
		{"UC-042/A11 env 0 is no ceiling", "0", 6, 8, 6},
		{"UC-042/A11 env negative is no ceiling", "-1", 6, 8, 6},
		{"UC-042/A11 env invalid is no ceiling", "many", 6, 8, 6},
		{"UC-042/A11 shadow capacity clamps", "", 6, 4, 4},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envDynamicMaxConcurrent, tc.env)
			loop, _ := loopWithRealRegistry(t, tc.capacity)
			loop.ceilings = experimentCeilingsFromEnv(testLogger())
			if got := loop.concurrentBudget(flags.ExperimentConfig{DynamicMaxConcurrent: tc.flag}); got != tc.want {
				t.Fatalf("concurrentBudget = %d, want %d", got, tc.want)
			}
			// The M7-4 proposer's sink draws the same budget (one helper, three callers).
			loop.configOverride = func(context.Context) flags.ExperimentConfig {
				return flags.ExperimentConfig{DynamicMaxConcurrent: tc.flag}
			}
			if got := loop.proposalSink().Budget(context.Background()); got != tc.want {
				t.Fatalf("proposal sink budget = %d, want %d", got, tc.want)
			}
		})
	}
}

// A set env max-concurrent no longer becomes the unresolved-flag fallback: the
// fallback is the compiled 3, then capped by the env (env 6 gives 3, env 2 gives 2).
// UC: UC-042 A11
func TestConcurrentBudget_UnresolvedFlagUsesCompiledDefaultCappedByEnv(t *testing.T) {
	for _, tc := range []struct {
		env  string
		want int
	}{{"6", 3}, {"2", 2}, {"", 3}} {
		t.Run("UC-042/A11 env="+tc.env, func(t *testing.T) {
			t.Setenv(envDynamicMaxConcurrent, tc.env)
			def := experimentDefaultsFromEnv()
			if def.DynamicMaxConcurrent != defaultDynamicMaxConcurrent {
				t.Fatalf("fallback = %d, want the compiled %d (the env is a ceiling, not the fallback)", def.DynamicMaxConcurrent, defaultDynamicMaxConcurrent)
			}
			loop, _ := loopWithRealRegistry(t, 8)
			loop.ceilings = experimentCeilingsFromEnv(testLogger())
			// An unresolved flag reads its def: the ExperimentDefaults value.
			if got := loop.concurrentBudget(flags.ExperimentConfig{DynamicMaxConcurrent: def.DynamicMaxConcurrent}); got != tc.want {
				t.Fatalf("budget = %d, want %d", got, tc.want)
			}
		})
	}
}

// The games budget is min(env ceiling, flag) where 0 or less = no limit on either
// side; env unset, 0 or invalid is no ceiling.
// UC: UC-043 A3
func TestMaxGamesBudget_EnvCeilingTable(t *testing.T) {
	cases := []struct {
		name string
		env  string // AGENT_EXPERIMENT_MAX_GAMES; "" = unset
		flag int
		want int // 0 = no limit
	}{
		{"UC-043/A3 env unset: flag default", "", 50, 50},
		{"UC-043/A3 env unset: flag long", "", 100, 100},
		{"UC-043/A3 env unset: flag unlimited", "", 0, 0},
		{"UC-043/A3 env caps a wider flag", "40", 100, 40},
		{"UC-043/A3 env caps an unlimited flag", "40", 0, 40},
		{"UC-043/A3 env caps a negative (unlimited) flag", "40", -1, 40},
		{"UC-043/A3 a narrower flag tightens the env", "40", 30, 30},
		{"UC-043/A3 env 0 is no ceiling", "0", 100, 100},
		{"UC-043/A3 env negative is no ceiling", "-5", 100, 100},
		{"UC-043/A3 env invalid is no ceiling", "lots", 100, 100},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envMaxGames, tc.env)
			loop := &experimentLoop{ceilings: experimentCeilingsFromEnv(testLogger())}
			if got := loop.maxGamesBudget(flags.ExperimentConfig{MaxGames: tc.flag}); got != tc.want {
				t.Fatalf("maxGamesBudget = %d, want %d", got, tc.want)
			}
		})
	}
}

// Each tick hands the capped games budget to the registry, and a flag write
// reaches the next tick.
// UC: UC-043 A3
func TestMaxGamesBudget_AppliedPerTick(t *testing.T) {
	loop, _ := loopWithRealRegistry(t, 8)
	loop.dynamic = nil // only the caps matter here
	loop.ceilings = experimentCeilings{MaxGames: 40}
	flag := 100
	loop.configOverride = func(context.Context) flags.ExperimentConfig {
		return flags.ExperimentConfig{Enabled: true, Tick: time.Hour, MaxGames: flag, ShadowEffectiveConcurrency: 1}
	}
	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.MaxGames(); got != 40 {
		t.Fatalf("registry games budget = %d, want 40 (env ceiling caps flag 100)", got)
	}
	flag = 30
	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.MaxGames(); got != 30 {
		t.Fatalf("registry games budget = %d after the flag write, want 30", got)
	}
}

// The retro-seeded thesis proposer declares only when AGENT_EXPERIMENT_PROPOSER_ENABLED
// is true AND experiment_proposer_enabled is on; a flag flip reaches the next tick.
// UC: UC-042 A3 BR-002
func TestRetroProposer_GateIsEnvAndFlag(t *testing.T) {
	cases := []struct {
		name     string
		env      string
		flagOn   bool
		wantLive int
	}{
		{"UC-042/A3 env true, flag on declares", "true", true, 1},
		{"UC-042/A3 env true, flag off declares nothing", "true", false, 0},
		{"UC-042/A3 env unset, flag on declares nothing", "", true, 0},
		{"UC-042/A3 env unset, flag off declares nothing", "", false, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(envProposerEnabled, tc.env)
			loop := proposerLoop(t)
			loop.proposer.Record(experiment.RetroSuggestion{Reason: "room faded"})
			cfg := dynCfg(5)
			cfg.ProposerEnabled = tc.flagOn
			loop.declareFromRetroIfEnabled(context.Background(), cfg)
			if got := loop.registry.LiveCount(); got != tc.wantLive {
				t.Fatalf("LiveCount = %d, want %d", got, tc.wantLive)
			}
		})
	}

	t.Run("UC-042/A3 a flag flip reaches the next tick", func(t *testing.T) {
		t.Setenv(envProposerEnabled, "true")
		loop := proposerLoop(t)
		loop.proposer.Record(experiment.RetroSuggestion{Reason: "room faded"})
		flagOn := false
		loop.configOverride = func(context.Context) flags.ExperimentConfig {
			cfg := dynCfg(5)
			cfg.DynamicEnabled = false
			cfg.ProposerEnabled = flagOn
			return cfg
		}
		loop.enabledOverride = func(context.Context) bool { return true }
		loop.allocateIfEnabled(context.Background())
		if got := loop.registry.LiveCount(); got != 0 {
			t.Fatalf("LiveCount = %d with the flag off, want 0", got)
		}
		flagOn = true
		loop.allocateIfEnabled(context.Background())
		if got := loop.registry.LiveCount(); got != 1 {
			t.Fatalf("LiveCount = %d after the flag flip, want 1", got)
		}
	})
}

// The M7-4 proposer obeys the same proposer gate: with a real text backend and the
// gate closed it makes no model call and charges no budget slot; opening the flag
// reaches the next game end.
// UC: UC-042 A5 BR-002
// NFR: NFR-011
func TestM74Proposer_GateClosedNoCallNoSpend(t *testing.T) {
	cases := []struct {
		name    string
		ceiling bool
		flagOn  bool
	}{
		{"UC-042/BR-002 env true, flag off", true, false},
		{"UC-042/BR-002 env unset, flag on", false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			budget := decision.NewLLMBudget()
			gate := realOnlyGate(1)
			calls := 0
			delegate := func(context.Context, llm.Prompt) (string, error) { calls++; return "not json", nil }
			p := newWiredProposer(sourceFor(delegate), nil, nil, decision.NewLLMAdmission(gate, budget, 0, "proposer", nil), nil)
			src := &liveGateFlags{enabled: true, experiments: true, proposer: tc.flagOn}
			cfg := experiment.ProposerConfig{Permit: proposerPermit(src, false, tc.ceiling), GameKind: "real"}

			res := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
			if res.Outcome != experiment.ProposeDisabled || res.Reason != experiment.SkipReasonProposerDisabled {
				t.Fatalf("outcome = (%q, %q), want (disabled, proposer_disabled)", res.Outcome, res.Reason)
			}
			if calls != 0 {
				t.Fatalf("model calls = %d with the proposer gate closed, want 0", calls)
			}
			if ok, reason := decision.NewLLMAdmission(gate, budget, 0, "proposer", nil).Admit(context.Background(), "real"); !ok {
				t.Fatalf("a closed-gate cycle charged the budget (%q)", reason)
			}
		})
	}

	t.Run("UC-042/BR-002 flag flip opens the next cycle", func(t *testing.T) {
		calls := 0
		delegate := func(context.Context, llm.Prompt) (string, error) { calls++; return "not json", nil }
		p := newWiredProposer(sourceFor(delegate), nil, nil, nil, nil)
		src := &liveGateFlags{enabled: true, experiments: true, proposer: false}
		cfg := experiment.ProposerConfig{Permit: proposerPermit(src, false, true), GameKind: "real"}
		p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg)
		src.proposer = true
		if got := p.ProposeOnce(context.Background(), experiment.Narrative{}, cfg).Outcome; got != experiment.ProposeUndecodable {
			t.Fatalf("outcome after the flip = %q, want undecodable (the model was called)", got)
		}
		if calls != 1 {
			t.Fatalf("model calls = %d, want 1", calls)
		}
	})
}

// graceCapture records the window of each scheduled completed-game grace timer.
type graceCapture struct {
	mu    sync.Mutex
	waits []time.Duration
}

func (g *graceCapture) schedule(d time.Duration, _ func()) func() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.waits = append(g.waits, d)
	return func() bool { return true }
}

// experiment_completed_grace_seconds is read when a completed game arms its grace
// (FR-150, FR-151, #1810): the `off` variant 0 arms nothing, and a flag write
// reaches the next completed game with no restart.
func TestCompletedGrace_ReadAtUseTime(t *testing.T) {
	capture := &graceCapture{}
	grace := 60 * time.Second
	loop := &experimentLoop{
		log:       testLogger(),
		afterFunc: capture.schedule,
		configOverride: func(context.Context) flags.ExperimentConfig {
			return flags.ExperimentConfig{CompletedGrace: grace}
		},
	}
	loop.armCompletedGrace("g1")
	grace = 0 // the `off` variant
	loop.armCompletedGrace("g2")
	grace = 30 * time.Second
	loop.armCompletedGrace("g3")

	want := []time.Duration{60 * time.Second, 30 * time.Second}
	if len(capture.waits) != len(want) || capture.waits[0] != want[0] || capture.waits[1] != want[1] {
		t.Fatalf("armed grace windows = %v, want %v (off arms nothing; each arm reads the live flag)", capture.waits, want)
	}
}

// With no flag client the grace falls back to the env-derived default (FR-151).
func TestCompletedGrace_FallsBackToEnv(t *testing.T) {
	t.Setenv(envCompletedGrace, "45")
	capture := &graceCapture{}
	loop := &experimentLoop{log: testLogger(), afterFunc: capture.schedule, expDefaults: experimentDefaultsFromEnv()}
	loop.armCompletedGrace("g1")
	if len(capture.waits) != 1 || capture.waits[0] != 45*time.Second {
		t.Fatalf("armed grace windows = %v, want [45s] (env fallback)", capture.waits)
	}
}
