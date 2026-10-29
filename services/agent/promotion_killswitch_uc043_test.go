package main

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/open-feature/go-sdk/openfeature"

	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/promote"
)

// promotion_killswitch_uc043_test.go covers the experiment loop's half of the
// kill-switch revert of an autonomous promotion (UC-043 A1, UC-045 step 5, #1704)
// and the live fitness.* thresholds of experiment fitness scoring (UC-043 step 4,
// D-F5, #1740), plus the game start the autonomous watch filters on (#1702).

// fakeKillSwitchPromotions records each RevertOnKillSwitch call and returns the
// configured reverts once.
type fakeKillSwitchPromotions struct {
	mu      sync.Mutex
	calls   int
	pending []promote.KillSwitchRevert
}

func (f *fakeKillSwitchPromotions) RevertOnKillSwitch(context.Context) []promote.KillSwitchRevert {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	out := f.pending
	f.pending = nil
	return out
}

// promotionEvents returns the promotion events of an experiment's journal.
func promotionEvents(t *testing.T, root, id string) []journal.Event {
	t.Helper()
	j, err := journal.Load(root, id)
	if err != nil {
		t.Fatalf("load journal %s: %v", id, err)
	}
	events, err := j.Events()
	if err != nil {
		t.Fatalf("journal events: %v", err)
	}
	var out []journal.Event
	for _, e := range events {
		if e.Kind == journal.KindPromotion {
			out = append(out, e)
		}
	}
	return out
}

// UC: UC-043 A1
// UC: UC-045 main
func TestUC043_A1_KillSwitchTickRevertsPromotions(t *testing.T) {
	t.Run("UC-043/A1 enabled off: revert journaled with reason kill_switch", func(t *testing.T) {
		loop, _, lc := loopForConfigTest(t, 8)
		root := t.TempDir()
		loop.registry = experiment.NewRegistry(experiment.RegistryConfig{
			Root: root, MaxShadowGames: 8, Spawner: &recordingSpawner{},
			Verdict: experiment.NewVerdict(3, 0.5, nil), Log: testLogger(),
		})
		agentOn := true
		loop.enabledOverride = func(context.Context) bool { return agentOn }
		lc.set(flags.ExperimentConfig{Enabled: true, ShadowEffectiveConcurrency: 4})
		ctx := context.Background()
		loop.allocateIfEnabled(ctx)
		live := loop.registry.Live()
		if len(live) != 1 {
			t.Fatalf("precondition: %d live experiments, want 1", len(live))
		}
		promotions := &fakeKillSwitchPromotions{pending: []promote.KillSwitchRevert{{
			ExperimentID: live[0], FlagKey: "nonstop.respawn_seconds",
			Outcome: promote.OutcomeReverted, Reason: promote.KillSwitchReason,
		}}}
		loop.promotions = promotions

		loop.allocateIfEnabled(ctx)
		if promotions.calls != 0 {
			t.Fatalf("kill-switch revert ran %d times with the agent on, want 0", promotions.calls)
		}

		agentOn = false
		loop.allocateIfEnabled(ctx)

		if promotions.calls != 1 {
			t.Fatalf("kill-switch revert ran %d times, want 1", promotions.calls)
		}
		events := promotionEvents(t, root, live[0])
		if len(events) != 1 || events[0].Promotion == nil ||
			events[0].Promotion.Outcome != "reverted" || events[0].Promotion.Reason != "kill_switch" {
			t.Fatalf("journaled promotion events = %+v, want one reverted / kill_switch", events)
		}
	})

	t.Run("UC-043/A1 enabled off with experiments_enabled off still reverts", func(t *testing.T) {
		loop, _, lc := loopForConfigTest(t, 8)
		loop.enabledOverride = func(context.Context) bool { return false }
		lc.set(flags.ExperimentConfig{Enabled: false})
		promotions := &fakeKillSwitchPromotions{}
		loop.promotions = promotions

		loop.allocateIfEnabled(context.Background())
		loop.allocateIfEnabled(context.Background())

		if promotions.calls != 2 {
			t.Fatalf("kill-switch revert ran %d times over 2 ticks, want 2 (every tick while off)", promotions.calls)
		}
	})
}

// fitnessFlagsEvaluator serves fitness.* int flags from a map; everything else
// falls back to the caller's default (enabled is on).
type fitnessFlagsEvaluator struct {
	ints map[string]int64
}

func (e fitnessFlagsEvaluator) BooleanValue(_ context.Context, k string, d bool, _ openfeature.EvaluationContext, _ ...openfeature.Option) (bool, error) {
	if k == "enabled" {
		return true, nil
	}
	return d, nil
}

func (e fitnessFlagsEvaluator) StringValue(_ context.Context, _ string, d string, _ openfeature.EvaluationContext, _ ...openfeature.Option) (string, error) {
	return d, nil
}

func (e fitnessFlagsEvaluator) IntValue(_ context.Context, k string, d int64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (int64, error) {
	if v, ok := e.ints[k]; ok {
		return v, nil
	}
	return d, nil
}

func (e fitnessFlagsEvaluator) FloatValue(_ context.Context, _ string, d float64, _ openfeature.EvaluationContext, _ ...openfeature.Option) (float64, error) {
	return d, nil
}

func (e fitnessFlagsEvaluator) ObjectValue(_ context.Context, _ string, d any, _ openfeature.EvaluationContext, _ ...openfeature.Option) (any, error) {
	return d, nil
}

// UC: UC-043 main
func TestUC043_Step4_FitnessUsesLiveThresholds(t *testing.T) {
	// A 90 s real game: below the default endurance minimum (120 s), above the
	// operator's live value (60 s).
	gc := withEffectiveDuration(realEnduranceGC("game_live_fit", 90))
	src := flagsFitnessSource{flags: flags.New(fitnessFlagsEvaluator{ints: map[string]int64{
		"fitness.endurance.min_session_seconds": 60,
	}}, testLogger())}
	static := defaultGameFitness(gc, "endurance")

	t.Run("UC-043/main step 4 shadow fitness scores with the live flags", func(t *testing.T) {
		live := gameFitnessWith(src)(gc, "endurance")
		if live <= static {
			t.Fatalf("live-threshold fitness = %v, static = %v: the live fitness.* flag was ignored", live, static)
		}
	})

	t.Run("UC-043/main step 7 real-game fitness scores with the live flags and records the start", func(t *testing.T) {
		store := experiment.NewFitnessStore(0)
		loop := loopWithStore(store)
		loop.thresholds = src

		loop.onGameEnd(gc)

		got := store.RecentReal("endurance", 1)
		if len(got) != 1 {
			t.Fatalf("recorded %d endurance samples, want 1", len(got))
		}
		if got[0].Fitness <= static {
			t.Fatalf("recorded real fitness = %v, static = %v: the live fitness.* flag was ignored", got[0].Fitness, static)
		}
		if want := time.Unix(1_000, 0); !got[0].StartedAt.Equal(want) {
			t.Fatalf("recorded StartedAt = %v, want the game's start %v", got[0].StartedAt, want)
		}
	})

	t.Run("UC-043/main step 4 the production loop is wired to the live flags", func(t *testing.T) {
		loop := buildExperimentLoop(src.flags, nil, nil, gameServiceForDeclarer(t), nil, testLogger())
		if live := loop.fitness(gc, "endurance"); live <= static {
			t.Fatalf("buildExperimentLoop fitness = %v, static = %v: not wired to the live flags", live, static)
		}
		if loop.fitnessThresholds().EnduranceMinSessionSeconds != 60 {
			t.Fatalf("buildExperimentLoop thresholds = %+v, want the live endurance minimum 60", loop.fitnessThresholds())
		}
	})
}

// UC: UC-043 main
func TestUC043_Step7_LongRealGameCountsAfterWrite(t *testing.T) {
	t.Run("UC-043/main step 7 a long 4-player game keeps its start past the timeline ring", func(t *testing.T) {
		start := time.Unix(50_000, 0)
		now := start
		store := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return now })
		var ended *gamecontext.GameContext
		store.OnGameEnd = func(gc gamecontext.GameContext) { ended = &gc }

		store.SetGameActive(true)
		store.SetGameKind(experiment.GameKindReal)
		serials := []string{"p1", "p2", "p3", "p4"}
		for i := 0; i < 200; i++ { // far more state deltas than the 64-entry timeline ring
			now = now.Add(time.Second)
			store.SetPlayerIntensity(serials[i%4], float64(i%10)/10+0.05)
		}
		store.SetGameActive(false)
		if ended == nil {
			t.Fatal("precondition: the store fired no game end")
		}
		for _, ev := range ended.Timeline {
			if ev.Kind == gamecontext.EventPhase && ev.Detail == "start" {
				t.Fatal("precondition: the start phase event is still in the timeline ring; the game is not long enough")
			}
		}

		fitness := experiment.NewFitnessStore(0)
		loop := loopWithStore(fitness)
		loop.onGameEnd(*ended)

		writeAt := start.Add(-time.Minute) // the promotion was written before this game started
		got := fitness.RecentRealSince("endurance", writeAt, 5)
		if len(got) != 1 {
			t.Fatalf("watch window holds %d samples of a game that started after the write, want 1", len(got))
		}
		if !got[0].StartedAt.Equal(start) {
			t.Fatalf("recorded StartedAt = %v, want the session start %v", got[0].StartedAt, start)
		}
	})
}
