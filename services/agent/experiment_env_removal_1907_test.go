package main

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
)

// experiment_env_removal_1907_test.go covers #1907 PR 2b: with the experiment and
// verdict env fallbacks gone, the live flags reach the first-tick seed declare,
// the C-034 env ceilings still bind, and the first refill does not wait the
// compiled 30 s tick.

// The first enabled tick applies the live verdict_min_n BEFORE the seed Declare,
// so the seed keeps its requested target_n instead of being raised to the
// compiled min-N the registry was built with (8).
// UC: UC-043 A3
func TestFirstTickSeed_UsesLiveVerdictMinN(t *testing.T) {
	t.Setenv("AGENT_VERDICT_MIN_N", "1") // ignored since #1907
	loop, _, lc := loopForConfigTest(t, 8)
	lc.setSeed(testSeed("nonstop.respawn_seconds", 6.0, 2))
	lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4, VerdictMinN: 1, VerdictMinPairs: 1})

	loop.allocateIfEnabled(context.Background())

	ids := loop.registry.Live()
	if len(ids) != 1 {
		t.Fatalf("live experiments = %v, want the one seed", ids)
	}
	view, ok := loop.registry.CompactView(ids[0])
	if !ok {
		t.Fatalf("no compact view for %s", ids[0])
	}
	if got := view.Intent.TargetNPerArm; got != 2 {
		t.Fatalf("seed target_n = %d, want 2 (live verdict_min_n=1 applied before the declare)", got)
	}
}

// The operator's AGENT_EXPERIMENT_MAX_GAMES ceiling (C-034, FR-152 f) still binds
// at registry construction and after every tick, below a wider flag.
// UC: UC-043 A3
func TestMaxGamesCeiling_BindsAtConstructionAndAfterTick(t *testing.T) {
	t.Setenv(envMaxGames, "2")
	reg := experiment.NewRegistry(experiment.RegistryConfig{Root: t.TempDir(), MaxShadowGames: 8, Spawner: &recordingSpawner{}, Log: testLogger()})
	if got := reg.MaxGames(); got != 2 {
		t.Fatalf("games budget at construction = %d, want the env ceiling 2", got)
	}

	loop, _ := loopWithRealRegistry(t, 8)
	loop.dynamic = nil
	loop.ceilings = experimentCeilingsFromEnv(testLogger())
	loop.configOverride = func(context.Context) flags.ExperimentConfig {
		return flags.ExperimentConfig{Enabled: true, Tick: time.Hour, MaxGames: 50, ShadowEffectiveConcurrency: 1}
	}
	loop.allocateIfEnabled(context.Background())
	if got := loop.registry.MaxGames(); got != 2 {
		t.Fatalf("games budget after a tick with flag 50 = %d, want the env ceiling 2", got)
	}
}

// fakeLoopTicker records the intervals run() arms and lets the test fire ticks.
type fakeLoopTicker struct {
	c       chan time.Time
	mu      sync.Mutex
	armed   []time.Duration
	stopped bool
}

func (f *fakeLoopTicker) Chan() <-chan time.Time { return f.c }
func (f *fakeLoopTicker) Reset(d time.Duration) {
	f.mu.Lock()
	f.armed = append(f.armed, d)
	f.mu.Unlock()
}
func (f *fakeLoopTicker) Stop() {
	f.mu.Lock()
	f.stopped = true
	f.mu.Unlock()
}
func (f *fakeLoopTicker) intervals() []time.Duration {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]time.Duration(nil), f.armed...)
}

// The first refill comes within firstTickCap even when the resolved tick is the
// compiled 30 s (flagd not yet synced); after that tick the ticker moves to the
// live experiment_tick_seconds, and a tick shorter than the cap is used as is.
// UC: UC-043 A1
func TestExperimentRun_FirstTickCappedThenLiveCadence(t *testing.T) {
	cases := []struct {
		name  string
		tick  time.Duration
		armed []time.Duration // first arm, then each Reset
	}{
		{"UC-043/A1 compiled 30s: first tick capped at 5s, then 30s", 30 * time.Second, []time.Duration{firstTickCap, 30 * time.Second}},
		{"UC-043/A1 live 3s: below the cap, no re-arm", 3 * time.Second, []time.Duration{3 * time.Second}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			loop, _, lc := loopForConfigTest(t, 8)
			lc.set(flags.ExperimentConfig{Tick: tc.tick}) // gates closed: every tick is inert
			ft := &fakeLoopTicker{c: make(chan time.Time)}
			loop.newTicker = func(d time.Duration) loopTicker {
				ft.Reset(d)
				return ft
			}
			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan struct{})
			go func() {
				loop.run(ctx)
				close(done)
			}()
			ft.c <- time.Now() // the second tick; the first ran inline
			ft.c <- time.Now() // a third tick proves the re-arm happened once
			cancel()
			<-done
			got := ft.intervals()
			if len(got) != len(tc.armed) {
				t.Fatalf("armed intervals = %v, want %v", got, tc.armed)
			}
			for i := range got {
				if got[i] != tc.armed[i] {
					t.Fatalf("armed intervals = %v, want %v", got, tc.armed)
				}
			}
		})
	}
}
