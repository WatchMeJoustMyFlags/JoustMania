package experiment

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
	"github.com/joustmania/agent/patterngame"
)

// queuePinner hands out pins in call order (the last one repeats) and counts calls.
type queuePinner struct {
	mu    sync.Mutex
	pins  []patterngame.Pin
	calls int
}

func (q *queuePinner) Pin(context.Context) patterngame.Pin {
	q.mu.Lock()
	defer q.mu.Unlock()
	i := q.calls
	if i >= len(q.pins) {
		i = len(q.pins) - 1
	}
	q.calls++
	return q.pins[i]
}

// patternSpawner records the PatternSpawn each Spawn ctx carries.
type patternSpawner struct {
	fakeSpawner
	mu       sync.Mutex
	patterns []*PatternSpawn
}

func (p *patternSpawner) Spawn(ctx context.Context, experimentID, arm string, seed uint64) (string, error) {
	ps, ok := PatternSpawnFrom(ctx)
	p.mu.Lock()
	if ok {
		p.patterns = append(p.patterns, &ps)
	} else {
		p.patterns = append(p.patterns, nil)
	}
	p.mu.Unlock()
	return p.fakeSpawner.Spawn(ctx, experimentID, arm, seed)
}

func testPin(t *testing.T, names ...string) patterngame.Pin {
	t.Helper()
	pin := patterngame.Pin{Selection: strings.Join(names, ","), MaxGameSeconds: 300}
	for i, n := range names {
		a, errs := patterngame.NewArchetype(n, map[string]any{
			"players": 4.0, "stream_rate_hz": 30.0, "base": 1.0 + float64(i)*0.01, "drift": 0.01, "amp": 0.1,
			"period_s": 3.0, "phase": 0.0, "burst_h": 0.0, "burst_period_s": 0.0,
			"d_base": 0.0, "d_drift": 0.01, "d_amp": 0.0, "d_phase": 0.25,
		})
		if len(errs) > 0 {
			t.Fatal(errs)
		}
		pin.Archetypes = append(pin.Archetypes, a)
	}
	return pin
}

func startExperiment(t *testing.T, r *Registry) string {
	t.Helper()
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatal(err)
	}
	if err := r.Start(context.Background(), id); err != nil {
		t.Fatal(err)
	}
	return id
}

func journalEvents(t *testing.T, r *Registry, id string) []journal.Event {
	t.Helper()
	j, err := journal.Load(r.root, id)
	if err != nil {
		t.Fatal(err)
	}
	evs, err := j.Events()
	if err != nil {
		t.Fatal(err)
	}
	return evs
}

func eventsOfKind(evs []journal.Event, kind journal.EventKind) []journal.Event {
	var out []journal.Event
	for _, e := range evs {
		if e.Kind == kind {
			out = append(out, e)
		}
	}
	return out
}

// UC: UC-046 main A6 BR-006 BR-010
func TestRegistryPatternPairsOneAtATime(t *testing.T) {
	t.Run("UC-046/main pins at first spawn, one pair per archetype, one game at a time, concludes when exhausted", func(t *testing.T) {
		spawner := &patternSpawner{}
		pinner := &queuePinner{pins: []patterngame.Pin{testPin(t, "arch_a", "arch_b")}}
		r := newTestRegistry(t, RegistryConfig{
			MaxShadowGames: 4, EffectiveConcurrency: 4, MaxGamesPerExperiment: -1,
			Spawner: spawner, Verdict: alwaysInconclusive{}, Patterns: pinner,
		})
		id := startExperiment(t, r)
		ctx := context.Background()

		var seeds []uint64
		for game := 0; game < 4; game++ {
			if n := r.AllocateAndSpawn(ctx); n != 1 {
				t.Fatalf("game %d: spawned %d, want exactly 1 (BR-010 concurrency 1 despite cap 4)", game, n)
			}
			if r.AllocateAndSpawn(ctx) != 0 {
				t.Fatalf("game %d: a second game spawned while a pattern game is in flight", game)
			}
			spawner.fakeSpawner.mu.Lock()
			call := spawner.fakeSpawner.calls[game]
			spawner.fakeSpawner.mu.Unlock()
			seeds = append(seeds, call.seed)
			ps := spawner.patterns[len(spawner.patterns)-1]
			wantArch := []string{"arch_a", "arch_a", "arch_b", "arch_b"}[game]
			if ps == nil || ps.Archetype.Name != wantArch || ps.PairIndex != game/2 || ps.MaxGameSeconds != 300 {
				t.Fatalf("game %d: pattern spawn = %+v, want %s pair %d", game, ps, wantArch, game/2)
			}
			if _, err := r.ConcludeGame(ctx, call.gameID, 0.5, 30); err != nil {
				t.Fatal(err)
			}
		}
		if seeds[0] != seeds[1] || seeds[2] != seeds[3] || seeds[0] == seeds[2] {
			t.Errorf("seeds = %v, want shared per pair (BR-006)", seeds)
		}
		if st, _ := r.Status(id); !st.IsTerminal() {
			t.Errorf("status = %s, want terminal after every archetype was used (A6)", st)
		}
		if r.AllocateAndSpawn(ctx) != 0 {
			t.Error("spawned after exhaustion")
		}
		if pinner.calls != 1 {
			t.Errorf("pinner called %d times, want once (pinned at first spawn)", pinner.calls)
		}

		evs := journalEvents(t, r, id)
		pinned := eventsOfKind(evs, journal.KindPatternPinned)
		if len(pinned) != 1 || fmt.Sprint(pinned[0].Pattern.Archetypes) != "[arch_a arch_b]" || pinned[0].Pattern.MaxGameSeconds != 300 {
			t.Fatalf("pattern_pinned = %+v", pinned)
		}
		assigned := eventsOfKind(evs, journal.KindGameAssigned)
		if len(assigned) != 4 || assigned[2].Pattern == nil || assigned[2].Pattern.Archetype != "arch_b" ||
			assigned[2].Pattern.ParamsDigest == "" || assigned[2].Pattern.RngSeed != seeds[2] {
			t.Fatalf("game_assigned pattern facts = %+v", assigned)
		}
	})
}

// UC: UC-046 A5
func TestRegistryPatternPinIsKept(t *testing.T) {
	t.Run("UC-046/A5 a flag change after the pin applies only to experiments not yet spawned", func(t *testing.T) {
		spawner := &patternSpawner{}
		pinner := &queuePinner{pins: []patterngame.Pin{testPin(t, "first"), testPin(t, "second")}}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, EffectiveConcurrency: 1, Spawner: spawner, Patterns: pinner})
		startExperiment(t, r)
		ctx := context.Background()
		r.AllocateAndSpawn(ctx)
		spawner.fakeSpawner.mu.Lock()
		gid := spawner.fakeSpawner.calls[0].gameID
		spawner.fakeSpawner.mu.Unlock()
		_, _ = r.ConcludeGame(ctx, gid, 0.5, 10)
		r.AllocateAndSpawn(ctx)
		for i, ps := range spawner.patterns {
			if ps == nil || ps.Archetype.Name != "first" {
				t.Fatalf("spawn %d archetype = %+v, want the pinned 'first'", i, ps)
			}
		}
		startExperiment(t, r)
		_, _ = r.ConcludeGame(ctx, spawner.fakeSpawner.calls[1].gameID, 0.5, 10)
		r.AllocateAndSpawn(ctx)
		last := spawner.patterns[len(spawner.patterns)-1]
		if last == nil || last.Archetype.Name != "second" {
			t.Fatalf("new experiment archetype = %+v, want 'second'", last)
		}
	})
}

// UC: UC-046 A1 A2 A4 A13 BR-010
func TestRegistryPatternDiscardsAndNoise(t *testing.T) {
	flowOf := map[string]string{
		patterngame.DiscardArchetypeInvalid: "A2",
		patterngame.DiscardSelectionSmall:   "A4",
		patterngame.DiscardModeUnsupported:  "A13",
	}
	for _, reason := range []string{patterngame.DiscardArchetypeInvalid, patterngame.DiscardSelectionSmall, patterngame.DiscardModeUnsupported} {
		t.Run("UC-046/"+flowOf[reason]+" discards with "+reason, func(t *testing.T) {
			spawner := &patternSpawner{}
			pin := patterngame.Pin{Selection: "x", DiscardReason: reason, Errors: []string{"why"}}
			r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, Spawner: spawner, Patterns: &queuePinner{pins: []patterngame.Pin{pin}}})
			id := startExperiment(t, r)
			if n := r.AllocateAndSpawn(context.Background()); n != 0 {
				t.Fatalf("spawned %d", n)
			}
			if st, _ := r.Status(id); st != StatusDiscarded {
				t.Fatalf("status = %s, want discarded", st)
			}
			outcomes := eventsOfKind(journalEvents(t, r, id), journal.KindOutcome)
			if len(outcomes) == 0 || !strings.HasPrefix(outcomes[len(outcomes)-1].Note, reason) {
				t.Errorf("outcome note = %+v, want reason %s", outcomes, reason)
			}
		})
	}
	t.Run("UC-046/A1 an empty selection discards the experiment; nothing spawns", func(t *testing.T) {
		spawner := &patternSpawner{}
		empty := patterngame.Pin{DiscardReason: patterngame.DiscardSelectionEmpty, Errors: []string{"empty"}}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 2, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{empty}}})
		id := startExperiment(t, r)
		if n := r.AllocateAndSpawn(context.Background()); n != 0 || spawner.count() != 0 {
			t.Fatalf("spawned %d", n)
		}
		if st, _ := r.Status(id); st != StatusDiscarded {
			t.Fatalf("status = %s, want discarded", st)
		}
	})
	t.Run("UC-046/BR-010 one shadow game at a time across pattern experiments", func(t *testing.T) {
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, EffectiveConcurrency: 4, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "p"), testPin(t, "q")}}})
		startExperiment(t, r)
		startExperiment(t, r)
		r.AllocateAndSpawn(context.Background())
		if spawner.count() != 1 {
			t.Fatalf("spawns = %d, want 1 (concurrency 1 for every experiment)", spawner.count())
		}
	})
}

// UC: UC-046 A12 BR-008
func TestRegistryPatternKillSwitch(t *testing.T) {
	t.Run("UC-046/A12 AbortAll force-ends in-flight pattern games with kill_switch", func(t *testing.T) {
		spawner := &patternSpawner{}
		tracker := patterngame.NewTracker()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 1, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "p")}}, PatternTracker: tracker})
		startExperiment(t, r)
		r.AllocateAndSpawn(context.Background())
		gid := spawner.fakeSpawner.calls[0].gameID
		aborted := 0
		tracker.Register(patterngame.Info{GameID: gid, Abort: func() { aborted++ }})
		r.AbortAll(context.Background(), "kill switch")
		if aborted != 1 {
			t.Fatalf("abort hook called %d times", aborted)
		}
		rec, _ := tracker.Finish(gid, patterngame.OutcomeAborted, patterngame.GameEvents{}, nil)
		if fmt.Sprint(rec.NonExactReasons) != "[kill_switch]" {
			t.Errorf("reasons = %v", rec.NonExactReasons)
		}
	})
}

// UC: UC-046 main BR-008 BR-014
func TestRegistryRecordPatternEnd(t *testing.T) {
	t.Run("UC-046/main the replay record is journaled as pattern_game_ended", func(t *testing.T) {
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2})
		id := startExperiment(t, r)
		a := testPin(t, "p").Archetypes[0]
		r.RecordPatternEnd(patterngame.Record{
			Info:             patterngame.Info{GameID: "game_1", ExperimentID: id, Arm: "control", PairIndex: 0, Archetype: a, Seed: 5, FlagDigest: "fd"},
			Outcome:          patterngame.OutcomeTimeout,
			Slots:            []patterngame.SlotRecord{{Slot: 0, SignalHash: 1, Checkpoints: []string{"c0"}}, {Slot: 1, SignalHash: 2}},
			EliminationOrder: []int{1},
			WinnerSlot:       -1,
		})
		r.RecordPatternEnd(patterngame.Record{Info: patterngame.Info{ExperimentID: "unknown"}})
		ended := eventsOfKind(journalEvents(t, r, id), journal.KindPatternGameEnded)
		if len(ended) != 1 {
			t.Fatalf("pattern_game_ended events = %d", len(ended))
		}
		f := ended[0].Pattern
		if f.Outcome != "timeout" || *f.WinnerSlot != -1 || fmt.Sprint(f.EliminationOrder) != "[1]" ||
			!*f.ReplayExact || f.ParamsDigest != a.Digest || f.FlagDigest != "fd" ||
			fmt.Sprint(f.SignalHashes) != "[0000000000000001 0000000000000002]" {
			t.Errorf("facts = %+v", f)
		}
		if summary := r.entries[id].journal.Summary(); summary.Arms["control"] != nil && summary.Arms["control"].Count != 0 {
			t.Error("the replay record folded a fitness sample")
		}
	})
}

// failingSpawner fails every spawn with err.
type failingSpawner struct{ err error }

func (f failingSpawner) Spawn(context.Context, string, string, uint64) (string, error) {
	return "", f.err
}

// UC: UC-046 A8 BR-015
func TestRegistryPatternFailedSpawnUsesNoArchetype(t *testing.T) {
	for name, err := range map[string]error{
		"blocked by a real game": fmt.Errorf("start rejected: Shadow games blocked while a real game is running"),
		"backpressure":           fmt.Errorf("at capacity: %w", ErrSpawnBackpressure),
	} {
		t.Run("UC-046/A8 "+name+" consumes no arm", func(t *testing.T) {
			r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 1,
				Spawner: failingSpawner{err: err}, Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "a", "b")}}})
			id := startExperiment(t, r)
			for i := 0; i < 20; i++ {
				r.AllocateAndSpawn(context.Background())
			}
			r.mu.Lock()
			cursor, st := r.entries[id].armCursor, r.entries[id].status
			r.mu.Unlock()
			if cursor != 0 || st != StatusRunning {
				t.Fatalf("after 20 failed ticks: armCursor=%d status=%s, want 0/running", cursor, st)
			}
		})
	}
}

// UC: UC-046 A6 A9 BR-015
func TestRegistryPatternReleasedArmRerunsPair(t *testing.T) {
	t.Run("UC-046/A9 a released arm reruns its whole pair; after 3 reruns the archetype is skipped and the experiment concludes", func(t *testing.T) {
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 1, MaxGamesPerExperiment: -1,
			Spawner: spawner, Verdict: alwaysInconclusive{}, Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "only")}}})
		id := startExperiment(t, r)
		ctx := context.Background()
		nextGame := func() (string, *PatternSpawn) {
			if n := r.AllocateAndSpawn(ctx); n != 1 {
				t.Fatalf("spawned %d, want 1", n)
			}
			spawner.fakeSpawner.mu.Lock()
			defer spawner.fakeSpawner.mu.Unlock()
			return spawner.fakeSpawner.calls[len(spawner.fakeSpawner.calls)-1].gameID, spawner.patterns[len(spawner.patterns)-1]
		}
		// Experimental concludes, control is released: the pair runs again from the experimental arm.
		g1, _ := nextGame()
		_, _ = r.ConcludeGame(ctx, g1, 0.5, 30)
		g2, _ := nextGame()
		r.ReleaseGame(g2, "preempted")
		spawner.fakeSpawner.mu.Lock()
		arm := spawner.fakeSpawner.calls[len(spawner.fakeSpawner.calls)-1].arm
		spawner.fakeSpawner.mu.Unlock()
		if arm != ArmControl {
			t.Fatalf("setup: second game arm = %s", arm)
		}
		g3, ps := nextGame()
		spawner.fakeSpawner.mu.Lock()
		arm3 := spawner.fakeSpawner.calls[len(spawner.fakeSpawner.calls)-1].arm
		spawner.fakeSpawner.mu.Unlock()
		if arm3 != ArmExperimental || ps.PairIndex != 0 {
			t.Fatalf("after a release the next game is %s pair %d, want experimental pair 0", arm3, ps.PairIndex)
		}
		// Three more releases exhaust the reruns: the archetype is skipped and the
		// experiment concludes in the next allocation pass, with nothing spawned.
		r.ReleaseGame(g3, "error")
		for i := 0; i < 2; i++ {
			g, _ := nextGame()
			r.ReleaseGame(g, "error")
		}
		before := spawner.count()
		r.AllocateAndSpawn(ctx)
		if st, _ := r.Status(id); !st.IsTerminal() || spawner.count() != before {
			t.Fatalf("status=%s spawns %d->%d, want terminal with no new spawn", st, before, spawner.count())
		}
		evs := journalEvents(t, r, id)
		var reruns, skips int
		for _, e := range eventsOfKind(evs, journal.KindDecision) {
			switch e.Note {
			case notePairRerun:
				reruns++
			case noteArchetypeSkipped:
				skips++
			}
		}
		if reruns != maxPairReruns || skips != 1 {
			t.Errorf("reruns=%d skips=%d, want %d/1", reruns, skips, maxPairReruns)
		}
	})
}

// UC: UC-046 A5 BR-015
func TestRegistryPatternRehydrate(t *testing.T) {
	t.Run("UC-046/A5 a restart restores the pin and resumes at the first unfinished pair", func(t *testing.T) {
		root := t.TempDir()
		spawner := &patternSpawner{}
		cfg := RegistryConfig{Root: root, MaxShadowGames: 2, EffectiveConcurrency: 1, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "a", "b")}}}
		r := newTestRegistry(t, cfg)
		id := startExperiment(t, r)
		ctx := context.Background()
		for i := 0; i < 2; i++ { // complete pair 0
			r.AllocateAndSpawn(ctx)
			spawner.fakeSpawner.mu.Lock()
			g := spawner.fakeSpawner.calls[len(spawner.fakeSpawner.calls)-1].gameID
			spawner.fakeSpawner.mu.Unlock()
			_, _ = r.ConcludeGame(ctx, g, 0.5, 30)
		}
		// A different selection now: the restored experiment must keep "a","b".
		spawner2 := &patternSpawner{}
		cfg2 := cfg
		cfg2.Spawner = spawner2
		cfg2.Patterns = &queuePinner{pins: []patterngame.Pin{testPin(t, "x", "y")}}
		r2 := newTestRegistry(t, cfg2)
		if err := r2.Rehydrate([]string{id}); err != nil {
			t.Fatal(err)
		}
		r2.AllocateAndSpawn(ctx)
		if len(spawner2.patterns) != 1 || spawner2.patterns[0].Archetype.Name != "b" || spawner2.patterns[0].PairIndex != 1 {
			t.Fatalf("resumed with %+v, want archetype b pair 1", spawner2.patterns)
		}
	})
	t.Run("UC-046/BR-015 a pattern experiment whose pin is gone is discarded", func(t *testing.T) {
		root := t.TempDir()
		r := newTestRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 2, Spawner: &fakeSpawner{}})
		id := startExperiment(t, r)
		r.AllocateAndSpawn(context.Background()) // a game assigned with no pin (seam without pinner)
		r2 := newTestRegistry(t, RegistryConfig{Root: root, MaxShadowGames: 2, Spawner: &patternSpawner{},
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "a")}}})
		if err := r2.Rehydrate([]string{id}); err != nil {
			t.Fatal(err)
		}
		if st, _ := r2.Status(id); st != StatusDiscarded {
			t.Fatalf("status = %s, want discarded (%s)", st, patterngame.DiscardPinLost)
		}
	})
}

// UC: UC-046 A3 BR-011 BR-013
func TestRegistryPatternTimeoutReportingAndDigest(t *testing.T) {
	t.Run("UC-046/A3 the conclusion reports timeouts per arm and flags a clear difference", func(t *testing.T) {
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2})
		id := startExperiment(t, r)
		a := testPin(t, "p").Archetypes[0]
		end := func(arm, outcome string) {
			r.RecordPatternEnd(patterngame.Record{Info: patterngame.Info{ExperimentID: id, Arm: arm, Archetype: a},
				Outcome: outcome, Censored: outcome == patterngame.OutcomeTimeout})
		}
		end(ArmExperimental, patterngame.OutcomeTimeout)
		end(ArmExperimental, patterngame.OutcomeTimeout)
		end(ArmControl, patterngame.OutcomeWinner)
		end(ArmControl, patterngame.OutcomeTimeout)
		_, _ = r.conclude(context.Background(), id, journal.Verdict{Outcome: OutcomeInconclusive})
		outcomes := eventsOfKind(journalEvents(t, r, id), journal.KindOutcome)
		note := outcomes[len(outcomes)-1].Note
		for _, want := range []string{"experimental 2/2 (100%)", "control 1/2 (50%)", "experimental arm timed out clearly more often"} {
			if !strings.Contains(note, want) {
				t.Errorf("conclusion %q lacks %q", note, want)
			}
		}
		ended := eventsOfKind(journalEvents(t, r, id), journal.KindPatternGameEnded)
		if len(ended) != 4 || ended[0].Pattern.Censored == nil || !*ended[0].Pattern.Censored {
			t.Errorf("timeout not journaled as censored: %+v", ended)
		}
	})
	t.Run("UC-046/BR-011 the arm's flag digest is journaled on game_assigned", func(t *testing.T) {
		spawner := &patternSpawner{}
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 1, Spawner: spawner,
			Patterns:   &queuePinner{pins: []patterngame.Pin{testPin(t, "p")}},
			FlagDigest: func(_ context.Context, id, arm string) string { return "digest-" + arm }})
		id := startExperiment(t, r)
		r.AllocateAndSpawn(context.Background())
		assigned := eventsOfKind(journalEvents(t, r, id), journal.KindGameAssigned)
		if len(assigned) != 1 || assigned[0].Pattern.FlagDigest != "digest-experimental" || spawner.patterns[0].FlagDigest != "digest-experimental" {
			t.Fatalf("assigned = %+v", assigned)
		}
	})
}

// UC: UC-046 main
func TestRegistryPatternSpanEvents(t *testing.T) {
	t.Run("UC-046/main the experiment span carries pattern_pinned, pattern_game and pattern_game_ended", func(t *testing.T) {
		spawner := &patternSpawner{}
		r, sr := rootSpanRegistry(t, RegistryConfig{MaxShadowGames: 2, EffectiveConcurrency: 1, Spawner: spawner,
			Patterns: &queuePinner{pins: []patterngame.Pin{testPin(t, "p")}}})
		id := startExperiment(t, r)
		r.AllocateAndSpawn(context.Background())
		a := testPin(t, "p").Archetypes[0]
		info := patterngame.Info{GameID: "game_1", ExperimentID: id, Arm: ArmExperimental, PairIndex: 0,
			Archetype: a, Seed: 9, Serials: []string{"S0", "S1", "S2", "S3"}, MaxGameSeconds: 300, FlagDigest: "fd"}
		r.RecordPatternStart(info)
		r.RecordPatternEnd(patterngame.Record{Info: info, Outcome: patterngame.OutcomeTimeout, Censored: true,
			OrderSource: patterngame.OrderFromEvents, EliminationOrder: []int{3, 2}, WinnerSlot: -1})
		_ = r.Abort(context.Background(), id, "test")

		want := map[string]map[string]string{
			"experiment.pattern_pinned":     {"pattern.archetypes": `["p"]`, "pattern.max_game_seconds": "300"},
			"experiment.pattern_game":       {"pattern.archetype": "p", "pattern.params_digest": a.Digest, "game.rng_seed": "9", "pattern.flag_digest": "fd"},
			"experiment.pattern_game_ended": {"pattern.outcome": "timeout", "pattern.censored": "true", "pattern.order_source": "events", "pattern.elimination_order": "[3,2]", "pattern.winner_slot": "-1"},
		}
		got := map[string]map[string]string{}
		for _, s := range rootSpans(sr) {
			for _, ev := range s.Events() {
				attrs := map[string]string{}
				for _, kv := range ev.Attributes {
					attrs[string(kv.Key)] = kv.Value.Emit()
				}
				got[ev.Name] = attrs
			}
		}
		for name, kvs := range want {
			for k, v := range kvs {
				if got[name][k] != v {
					t.Errorf("%s %s = %q, want %q", name, k, got[name][k], v)
				}
			}
		}
	})
}
