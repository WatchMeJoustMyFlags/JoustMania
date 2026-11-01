package gamewindow

import (
	"io"
	"log/slog"
	"reflect"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/patterngame"
)

// Realistic controller serials: PS Move serials are Bluetooth MAC addresses on
// real hardware (with and without separators, upper and lower case), and the mock
// controller manager uses MOCKnnnn.
var realisticSerials = []string{
	"00:06:f7:c9:a1:b2",
	"E0:AE:5E:12:34:56",
	"a0ab515f3c10",
	"MOCK0001",
}

func f64(v float64) *float64 { return &v }
func intp(v int) *int        { return &v }

// realGame builds a real-game summary full of per-controller fields: four arcs keyed
// by realistic serials (three eliminated, one survivor with a near-death), dominance
// naming the survivor, and interventions targeting serials.
func realGame() gamesummary.Summary {
	s := realisticSerials
	return gamesummary.Summary{
		GameID:               "game_4f2a9c1e7b3d",
		GameKind:             gamecontext.GameKindReal,
		GameMode:             "JoustFFA",
		DurationSeconds:      f64(92),
		PlayerCount:          4,
		EliminationCount:     3,
		SurvivorCount:        1,
		InterventionsApplied: 2,
		InterventionsBlocked: 1,
		PlayerArcs: []gamesummary.PlayerArc{
			{Serial: s[0], Eliminated: true, EliminationOrder: 1, NearDeathRatio: f64(1.2)},
			{Serial: s[1], Eliminated: true, EliminationOrder: 2, NearDeathRatio: f64(1.1)},
			{Serial: s[2], Eliminated: true, EliminationOrder: 3},
			{Serial: s[3], NearDeathRatio: f64(0.85)},
		},
		EliminationSpread: []gamesummary.EliminationEvent{
			{Serial: s[0], Order: 1}, {Serial: s[1], Order: 2}, {Serial: s[2], Order: 3},
		},
		Dominance: gamesummary.Dominance{Dominated: true, Serial: s[3], Heuristic: "survivor_outlasted_mean_2x", Margin: 2.5},
		Interventions: []gamesummary.InterventionOutcome{
			{Intervention: "speed_up", TargetSerial: s[0], Dispatched: true,
				Effect: &gamesummary.InterventionEffect{MeanIntensityDelta: f64(0.10), ActivePlayersDelta: intp(1)}},
			{Intervention: "speed_up", Dispatched: true,
				Effect: &gamesummary.InterventionEffect{MeanIntensityDelta: f64(0.30)}},
			{Intervention: "grant_shield", TargetSerial: s[3], Dispatched: false},
		},
	}
}

const (
	expA = "exp-7f3a"
	expB = "exp-91c2"
)

// shadowGame builds a shadow-game summary of experiment exp, as rich in
// per-controller fields as realGame.
func shadowGame(exp, arm string, players int) gamesummary.Summary {
	g := realGame()
	g.GameKind = gamecontext.GameKindShadow
	g.ExperimentID = exp
	g.Arm = arm
	g.PlayerCount = players
	return g
}

// patternRecord builds a finished pattern game whose controllers carry MAC-like serials.
func patternRecord() patterngame.Record {
	return patterngame.Record{
		Info: patterngame.Info{
			GameID:       "game_9e8d7c6b5a41",
			ExperimentID: expA,
			Arm:          "treatment",
			PairIndex:    0,
			Archetype:    patterngame.Archetype{Name: "close_race", Digest: "1a2b3c4d5e6f7081", StreamRateHz: 60},
			Serials:      []string{"00:06:f7:c9:a1:b2", "E0:AE:5E:12:34:56", "a0ab515f3c10"},
		},
		Outcome: patterngame.OutcomeWinner,
		Slots: []patterngame.SlotRecord{
			{Slot: 2, Serial: "a0ab515f3c10", FramesDelivered: 1200},
			{Slot: 0, Serial: "00:06:f7:c9:a1:b2", FramesDelivered: 600},
			{Slot: 1, Serial: "E0:AE:5E:12:34:56", FramesDelivered: 900},
		},
		EliminationOrder: []int{0, 1},
		WinnerSlot:       2,
	}
}

func assertNoSerial(t *testing.T, block string, serials []string) {
	t.Helper()
	lower := strings.ToLower(block)
	for _, serial := range serials {
		if strings.Contains(lower, strings.ToLower(serial)) {
			t.Errorf("rendered block contains serial %q:\n%s", serial, block)
		}
	}
	for _, marker := range []string{"dominated_by", "->", "serial"} {
		if strings.Contains(lower, marker) {
			t.Errorf("rendered block contains per-controller marker %q:\n%s", marker, block)
		}
	}
}

// UC: UC-042 BR-004
func TestWindow_RenderedBlockHasNoSerial(t *testing.T) {
	t.Run("UC-042/BR-004 real game", func(t *testing.T) {
		s := NewStore()
		s.Record(realGame())
		assertNoSerial(t, Render(s.SnapshotFor(gamecontext.GameKindReal, "")), realisticSerials)
	})
	t.Run("UC-042/BR-006 pattern game", func(t *testing.T) {
		s := NewStore()
		s.Record(realGame())
		s.RecordPattern(patternRecord())
		block := Render(s.SnapshotFor(gamecontext.GameKindShadow, expA))
		assertNoSerial(t, block, append(realisticSerials, patternRecord().Serials...))
		if !strings.Contains(block, "archetype=close_race params=1a2b3c4d5e6f7081") {
			t.Errorf("pattern game must be keyed by archetype and params digest:\n%s", block)
		}
	})
}

// allowedStrings lists the only string fields the window's types may have: none of
// them can carry a serial or a name (UC-042 BR-004).
var allowedStrings = map[string]bool{
	"GameAggregate.Mode":         true,
	"GameAggregate.Outcome":      true,
	"InterventionAggregate.Type": true,
	"PatternGame.Archetype":      true,
	"PatternGame.ParamsDigest":   true,
	"PatternGame.Outcome":        true,
	"PatternGame.Arm":            true,
	"ShadowGame.Arm":             true,
	// Closed vocabularies (decide backends, prediction sources and templates), UC-042 A12.
	"PredictionAggregate.Backend":  true,
	"PredictionAggregate.Source":   true,
	"PredictionAggregate.Template": true,
}

// identityViolations walks typ and returns every field path that could carry a
// serial or a name: a string at any depth (through pointers, slices, arrays and
// map keys or values) that is not allowlisted, any map, and any interface.
func identityViolations(typ reflect.Type, path string, seen map[reflect.Type]bool) []string {
	var out []string
	switch typ.Kind() {
	case reflect.Pointer, reflect.Slice, reflect.Array:
		return identityViolations(typ.Elem(), path, seen)
	case reflect.Map:
		out = append(out, path+" (map)")
		out = append(out, identityViolations(typ.Key(), path, seen)...)
		return append(out, identityViolations(typ.Elem(), path, seen)...)
	case reflect.Interface:
		return []string{path + " (interface)"}
	case reflect.String:
		if !allowedStrings[path] {
			return []string{path + " (string)"}
		}
		return nil
	case reflect.Struct:
		if seen[typ] {
			return nil
		}
		seen[typ] = true
		for i := 0; i < typ.NumField(); i++ {
			f := typ.Field(i)
			out = append(out, identityViolations(f.Type, typ.Name()+"."+f.Name, seen)...)
		}
	}
	return out
}

// UC: UC-042 BR-004 BR-006
func TestWindow_TypesHoldNoPerControllerIdentity(t *testing.T) {
	t.Run("UC-042/BR-004 window types", func(t *testing.T) {
		for _, v := range identityViolations(reflect.TypeOf(Window{}), "Window", map[reflect.Type]bool{}) {
			t.Errorf("UC-042/BR-004: field %s could carry a serial or a name", v)
		}
	})
	t.Run("UC-042/BR-004 guard catches wrapped strings", func(t *testing.T) {
		type mutant struct {
			Serials []string
			Owner   *string
			Fixed   [2]string
			Any     any
			ByID    map[int]int
			Nested  []struct{ Name *string }
		}
		got := identityViolations(reflect.TypeOf(mutant{}), "mutant", map[reflect.Type]bool{})
		if len(got) != 6 {
			t.Errorf("guard found %d violations, want 6: %v", len(got), got)
		}
	})
}

// UC: UC-042 BR-004
func TestWindow_AggregatesComputed(t *testing.T) {
	t.Run("UC-042/BR-004 winner game", func(t *testing.T) {
		agg := FromSummary(realGame())
		if agg.Mode != "JoustFFA" || agg.PlayerCount != 4 || *agg.DurationSeconds != 92 {
			t.Errorf("mode/players/duration = %q/%d/%v", agg.Mode, agg.PlayerCount, *agg.DurationSeconds)
		}
		if agg.Outcome != OutcomeWinner {
			t.Errorf("outcome = %q, want %q", agg.Outcome, OutcomeWinner)
		}
		if agg.DeathRate == nil || *agg.DeathRate != 0.75 {
			t.Errorf("death rate = %v, want 0.75", agg.DeathRate)
		}
		// Three players have an observed closest call; one survived at >= 0.8.
		if agg.NearDeathRate == nil || *agg.NearDeathRate != 1.0/3.0 {
			t.Errorf("near-death rate = %v, want 1/3", agg.NearDeathRate)
		}
		if len(agg.Interventions) != 2 {
			t.Fatalf("intervention types = %d, want 2", len(agg.Interventions))
		}
		shield, speed := agg.Interventions[0], agg.Interventions[1]
		if shield.Type != "grant_shield" || shield.Applied != 0 || shield.Blocked != 1 || shield.MeanIntensityDelta != nil {
			t.Errorf("grant_shield = %+v", shield)
		}
		if speed.Type != "speed_up" || speed.Applied != 0 || speed.Blocked != 0 {
			t.Errorf("speed_up = %+v", speed)
		}
		if speed.MeanIntensityDelta == nil || *speed.MeanIntensityDelta < 0.1999 || *speed.MeanIntensityDelta > 0.2001 {
			t.Errorf("speed_up mean intensity delta = %v, want 0.20", speed.MeanIntensityDelta)
		}
		if speed.MeanActiveDelta == nil || *speed.MeanActiveDelta != 1 {
			t.Errorf("speed_up mean active delta = %v, want 1", speed.MeanActiveDelta)
		}
	})
	t.Run("UC-042/BR-004 outcome types", func(t *testing.T) {
		cases := []struct {
			name string
			arcs []gamesummary.PlayerArc
			want string
		}{
			{"tie", []gamesummary.PlayerArc{{Eliminated: true}, {Eliminated: true}}, OutcomeTie},
			{"multiple survivors", []gamesummary.PlayerArc{{}, {}, {Eliminated: true}}, OutcomeMultipleSurvivors},
			{"no players", nil, OutcomeUnknown},
		}
		for _, tc := range cases {
			survivors := 0
			for _, a := range tc.arcs {
				if !a.Eliminated {
					survivors++
				}
			}
			got := FromSummary(gamesummary.Summary{GameKind: gamecontext.GameKindReal, PlayerArcs: tc.arcs,
				PlayerCount: len(tc.arcs), SurvivorCount: survivors, EliminationCount: len(tc.arcs) - survivors})
			if got.Outcome != tc.want {
				t.Errorf("%s: outcome = %q, want %q", tc.name, got.Outcome, tc.want)
			}
		}
		if FromSummary(gamesummary.Summary{}).DeathRate != nil {
			t.Error("death rate must be nil for a game with no players")
		}
	})
	t.Run("UC-042/BR-004 rendered line", func(t *testing.T) {
		s := NewStore()
		s.Record(realGame())
		want := "PRIOR GAMES (real games only, most recent last; game-level aggregates, no player identity):\n" +
			"  1. JoustFFA duration=92.0s players=4 outcome=winner death_rate=0.75 near_death_rate=0.33\n" +
			"     intervention=grant_shield applied=0 blocked=1\n" +
			"     intervention=speed_up applied=0 blocked=0 [mean_intensity_delta=0.20 mean_active_delta=1.0]"
		if got := Render(s.SnapshotFor(gamecontext.GameKindReal, "")); got != want {
			t.Errorf("Render =\n%s\nwant\n%s", got, want)
		}
	})
}

// UC: UC-042 BR-005
func TestWindow_RealWindowUnaffectedByShadowGames(t *testing.T) {
	s := NewStore()
	s.Record(shadowGame(expA, "control", 4))
	s.Record(shadowGame("", "", 4)) // shadow game without an experiment: no linked set
	for _, kind := range []string{"", "primary"} {
		g := realGame()
		g.GameKind = kind
		s.Record(g)
	}
	if s.Len() != 0 {
		t.Errorf("UC-042/BR-005: non-real games entered the real window: Len = %d", s.Len())
	}
	if got := Render(s.SnapshotFor(gamecontext.GameKindReal, "")); !strings.Contains(got, "(no prior games)") || strings.Contains(got, "LINKED") {
		t.Errorf("real game must see an empty real window and no linked set, got:\n%s", got)
	}
	if s.LinkedSets() != 1 {
		t.Errorf("linked sets = %d, want 1 (only the game with an experiment)", s.LinkedSets())
	}
	s.Record(realGame())
	w := s.SnapshotFor(gamecontext.GameKindShadow, expA)
	if len(w.Games) != 1 || len(w.Linked.Games) != 1 {
		t.Errorf("shadow view = %d real + %d linked, want 1 + 1", len(w.Games), len(w.Linked.Games))
	}
}

// UC: UC-042 BR-005
func TestWindow_LinkedSetIsolation(t *testing.T) {
	s := NewStore()
	s.Record(shadowGame(expA, "control", 3))
	s.Record(shadowGame(expA, "treatment", 5))
	s.Record(shadowGame(expB, "control", 7))
	rec := patternRecord()
	rec.ExperimentID = expB
	s.RecordPattern(rec)

	t.Run("UC-042/BR-005 next game of the same experiment sees its history", func(t *testing.T) {
		w := s.SnapshotFor(gamecontext.GameKindShadow, expA)
		if w.Linked == nil || len(w.Linked.Games) != 2 {
			t.Fatalf("expA linked set = %+v, want 2 games", w.Linked)
		}
		if w.Linked.Games[0].Arm != "control" || w.Linked.Games[1].Game.PlayerCount != 5 {
			t.Errorf("expA history = %+v", w.Linked.Games)
		}
		block := Render(w)
		if !strings.Contains(block, "LINKED SHADOW GAMES") || !strings.Contains(block, "  2. arm=treatment JoustFFA") {
			t.Errorf("linked section missing:\n%s", block)
		}
		assertNoSerial(t, block, realisticSerials)
	})
	t.Run("UC-042/BR-005 other experiments are invisible", func(t *testing.T) {
		a := Render(s.SnapshotFor(gamecontext.GameKindShadow, expA))
		if strings.Contains(a, "players=7") || strings.Contains(a, "PATTERN-DRIVEN") {
			t.Errorf("expA sees expB's games:\n%s", a)
		}
		b := s.SnapshotFor(gamecontext.GameKindShadow, expB)
		if len(b.Linked.Games) != 1 || len(b.Linked.PatternGames) != 1 {
			t.Errorf("expB linked set = %+v", b.Linked)
		}
		if w := s.SnapshotFor(gamecontext.GameKindShadow, "exp-unrelated"); w.Linked != nil {
			t.Errorf("an unrelated experiment sees a linked set: %+v", w.Linked)
		}
	})
}

// UC: UC-042 BR-005
func TestWindow_LinkedSetEvictedOnConclusion(t *testing.T) {
	s := NewStore()
	s.Record(shadowGame(expA, "control", 3))
	s.RecordPattern(patternRecord())
	s.Record(shadowGame(expB, "control", 4))
	s.Record(realGame())
	s.Forget(expA)
	if w := s.SnapshotFor(gamecontext.GameKindShadow, expA); w.Linked != nil {
		t.Errorf("UC-042/BR-005: concluded experiment still has history: %+v", w.Linked)
	}
	// A game of the concluded experiment that ends late does not recreate the set.
	s.Record(shadowGame(expA, "control", 3))
	s.RecordPattern(patternRecord())
	if s.LinkedSets() != 1 {
		t.Errorf("linked sets = %d, want 1 (expB only)", s.LinkedSets())
	}
	if w := s.SnapshotFor(gamecontext.GameKindShadow, expB); w.Linked == nil || len(w.Linked.Games) != 1 || len(w.Games) != 1 {
		t.Errorf("Forget(expA) touched expB or the real window: %+v", w)
	}
}

// UC: UC-042 BR-005 BR-006
func TestWindow_EvictsAtTwenty(t *testing.T) {
	t.Run("UC-042/BR-005 real games", func(t *testing.T) {
		s := NewStore()
		for i := 1; i <= 25; i++ {
			s.Record(gamesummary.Summary{GameKind: gamecontext.GameKindReal, PlayerCount: i})
		}
		games := s.SnapshotFor(gamecontext.GameKindReal, "").Games
		if len(games) != RetentionCap || RetentionCap != 20 {
			t.Fatalf("held %d games, want 20", len(games))
		}
		if games[0].PlayerCount != 6 || games[19].PlayerCount != 25 {
			t.Errorf("window = games %d..%d, want 6..25 (oldest evicted)", games[0].PlayerCount, games[19].PlayerCount)
		}
	})
	t.Run("UC-042/BR-005 linked set games", func(t *testing.T) {
		s := NewStore()
		for i := 1; i <= 21; i++ {
			s.Record(shadowGame(expA, "control", i))
		}
		g := s.SnapshotFor(gamecontext.GameKindShadow, expA).Linked.Games
		if len(g) != 20 || g[0].Game.PlayerCount != 2 {
			t.Errorf("linked set = %d games starting at %d, want 20 starting at 2", len(g), g[0].Game.PlayerCount)
		}
	})
	t.Run("UC-042/BR-005 linked set count is bounded", func(t *testing.T) {
		s := NewStore()
		for i := 0; i <= MaxLinkedSets; i++ {
			s.Record(shadowGame("exp-"+strconv.Itoa(i), "control", 2))
		}
		if s.LinkedSets() != MaxLinkedSets {
			t.Errorf("linked sets = %d, want %d", s.LinkedSets(), MaxLinkedSets)
		}
		if w := s.SnapshotFor(gamecontext.GameKindShadow, "exp-0"); w.Linked != nil {
			t.Error("the oldest linked set must be evicted past MaxLinkedSets")
		}
	})
	t.Run("UC-042/BR-006 pattern games", func(t *testing.T) {
		s := NewStore()
		for i := 1; i <= 21; i++ {
			rec := patternRecord()
			rec.Archetype.Name = "a" + strconv.Itoa(i)
			s.RecordPattern(rec)
		}
		pg := s.SnapshotFor(gamecontext.GameKindShadow, expA).Linked.PatternGames
		if len(pg) != 20 || pg[0].Archetype != "a2" || pg[19].Archetype != "a21" {
			t.Errorf("pattern window = %d games starting %q", len(pg), pg[0].Archetype)
		}
	})
}

// UC: UC-042 BR-006
func TestWindow_PatternRowsKeyedByArchetype(t *testing.T) {
	t.Run("UC-042/BR-006 real-controller rows never cross games", func(t *testing.T) {
		s := NewStore()
		s.Record(realGame())
		w := s.SnapshotFor(gamecontext.GameKindReal, "")
		if w.Linked != nil {
			t.Errorf("a real game produced pattern rows: %+v", w.Linked)
		}
		if block := Render(w); strings.Contains(block, "slot=") || strings.Contains(block, "PATTERN-DRIVEN") {
			t.Errorf("real game rendered per-player rows:\n%s", block)
		}
	})
	t.Run("UC-042/BR-006 pattern rows by slot", func(t *testing.T) {
		pg := FromPatternRecord(patternRecord())
		want := []PatternPlayer{
			{Slot: 0, EliminationPlace: 1, SurvivalSeconds: f64(10)},
			{Slot: 1, EliminationPlace: 2, SurvivalSeconds: f64(15)},
			{Slot: 2, Won: true, SurvivalSeconds: f64(20)},
		}
		if !reflect.DeepEqual(pg.Players, want) {
			t.Errorf("rows = %+v, want %+v", pg.Players, want)
		}
		s := NewStore()
		s.RecordPattern(patternRecord())
		block := Render(s.SnapshotFor(gamecontext.GameKindShadow, expA))
		for _, line := range []string{
			"  1. arm=treatment pair=0 archetype=close_race params=1a2b3c4d5e6f7081 outcome=winner censored=false",
			"     slot=0 out=1 survival=10.0s",
			"     slot=2 won survival=20.0s",
		} {
			if !strings.Contains(block, line) {
				t.Errorf("block missing %q:\n%s", line, block)
			}
		}
	})
	t.Run("UC-042/BR-006 mixed game rows only for pattern controllers", func(t *testing.T) {
		// A game with a real controller beside the pattern ones: the replay record
		// lists the pattern controllers only, so only they become rows.
		rec := patternRecord()
		rec.Slots = rec.Slots[:2] // slots 2 and 0 are pattern; the real one is absent
		rec.WinnerSlot = -1
		pg := FromPatternRecord(rec)
		if len(pg.Players) != 2 || pg.Players[0].Slot != 0 || pg.Players[1].Slot != 2 {
			t.Errorf("rows = %+v, want slots 0 and 2 only", pg.Players)
		}
		g := realGame() // the same game's summary carries the real controller's serial
		s := NewStore()
		s.Record(g)
		s.RecordPattern(rec)
		assertNoSerial(t, Render(s.SnapshotFor(gamecontext.GameKindShadow, expA)), append(realisticSerials, rec.Serials...))
	})
}

// UC: UC-042 BR-005
func TestWindow_ConcurrentRecordAndSnapshot(t *testing.T) {
	s := NewStore()
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(2)
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				s.Record(realGame())
				s.RecordPattern(patternRecord())
			}
		}()
		go func() {
			defer wg.Done()
			for j := 0; j < 50; j++ {
				_ = Render(s.SnapshotFor(gamecontext.GameKindShadow, expA))
			}
		}()
	}
	wg.Wait()
	if s.Len() != RetentionCap {
		t.Errorf("Len = %d, want %d", s.Len(), RetentionCap)
	}
}

// UC: UC-042 BR-004
func TestWindow_AggregatesSurviveTimelineOverflow(t *testing.T) {
	// Regression (#1421 review): eliminations and interventions early in a game fall
	// out of the 64-entry timeline once later intensity changes add state deltas. The
	// aggregates must still count them, from the session's unbounded truth.
	clock := time.Date(2026, 10, 16, 12, 0, 0, 0, time.UTC)
	store := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	window := NewStore()
	coord := gamesummary.NewCoordinator(slog.New(slog.NewTextHandler(io.Discard, nil)))
	coord.SetObserver(window)
	store.OnGameEnd = coord.OnGameEnd

	store.SetGameKind(gamecontext.GameKindReal)
	store.SetGameActive(true)
	store.AdoptSessionID("game_overflow")
	for _, serial := range realisticSerials {
		store.SetPlayerIntensity(serial, 0.5)
	}
	for i := 0; i < 3; i++ {
		clock = clock.Add(time.Second)
		store.SetEliminationOrder(realisticSerials[i], i+1)
		store.AppendInterventionEvent("speed_up", "", false)
		store.RecordInterventionApply("speed_up", gamecontext.ApplyOutcomeApplied)
	}
	store.AppendInterventionEvent("grant_shield", realisticSerials[3], true)
	for i := 0; i < 100; i++ {
		clock = clock.Add(100 * time.Millisecond)
		store.SetPlayerIntensity(realisticSerials[3], 0.5+float64(i)/1000)
	}
	for _, e := range store.Snapshot().Timeline {
		if e.Kind == gamecontext.EventElimination && e.Serial == realisticSerials[0] {
			t.Fatal("test setup: the first elimination is still on the timeline; the ring did not overflow")
		}
	}
	clock = clock.Add(time.Second)
	store.SetGameActive(false)

	w := window.SnapshotFor(gamecontext.GameKindReal, "")
	if len(w.Games) != 1 {
		t.Fatalf("window holds %d games, want 1", len(w.Games))
	}
	g := w.Games[0]
	if g.PlayerCount != 4 || g.Outcome != OutcomeWinner || g.DeathRate == nil || *g.DeathRate != 0.75 {
		t.Errorf("UC-042/BR-004: players=%d outcome=%q death_rate=%v, want 4 / winner / 0.75",
			g.PlayerCount, g.Outcome, g.DeathRate)
	}
	want := []InterventionAggregate{
		{Type: "grant_shield", Blocked: 1},
		{Type: "speed_up", Applied: 3},
	}
	if len(g.Interventions) != 2 ||
		g.Interventions[0].Type != want[0].Type || g.Interventions[0].Blocked != 1 || g.Interventions[0].Applied != 0 ||
		g.Interventions[1].Type != want[1].Type || g.Interventions[1].Applied != 3 || g.Interventions[1].Blocked != 0 {
		t.Errorf("UC-042/BR-004: interventions = %+v, want %+v", g.Interventions, want)
	}
	assertNoSerial(t, Render(w), realisticSerials)
}

// UC: UC-042 Post-S-1
func TestWindow_RecordedGameAggregatesAreInTheWindow(t *testing.T) {
	t.Run("UC-042/Post-S-1 a finished real game's aggregates are in the cross-game window", func(t *testing.T) {
		s := NewStore()
		s.Record(realGame())
		w := s.SnapshotFor(gamecontext.GameKindReal, "")
		if len(w.Games) != 1 {
			t.Fatalf("window games = %d, want 1", len(w.Games))
		}
		g := w.Games[0]
		want := FromSummary(realGame())
		if g.Outcome != want.Outcome || g.PlayerCount != 4 || g.Mode != "JoustFFA" || g.DeathRate == nil || *g.DeathRate != 0.75 {
			t.Fatalf("window aggregate = %+v, want the game's aggregates (%+v)", g, want)
		}
	})
}

// UC: UC-042 BR-004
// A dispatched decision that the actuation gate turned into a no-op, or whose
// write failed, is not applied; the whole-game tally decides (#1778).
func TestWindow_InterventionsCountAppliedNotDispatched(t *testing.T) {
	t.Run("UC-042/BR-004 dispatched but gated off is blocked, not applied", func(t *testing.T) {
		g := realGame()
		g.InterventionsByType = []gamesummary.InterventionTypeCount{
			// 5 passed the permission chain; 2 written, 2 no-ops of
			// interventions_enabled, 1 failed write; 1 more refused by the chain.
			{Type: "speed_up", Dispatched: 5, Blocked: 1, Applied: 2, GatedOff: 2},
			{Type: "grant_shield", Dispatched: 3, GatedOff: 3},
		}
		agg := FromSummary(g)
		got := map[string]InterventionAggregate{}
		for _, a := range agg.Interventions {
			got[a.Type] = a
		}
		if a := got["speed_up"]; a.Applied != 2 || a.Blocked != 3 {
			t.Errorf("speed_up = applied %d blocked %d, want 2 / 3", a.Applied, a.Blocked)
		}
		if a := got["grant_shield"]; a.Applied != 0 || a.Blocked != 3 {
			t.Errorf("grant_shield = applied %d blocked %d, want 0 / 3 (all dispatched were no-ops)", a.Applied, a.Blocked)
		}
		s := NewStore()
		s.Record(g)
		out := Render(s.SnapshotFor(gamecontext.GameKindReal, ""))
		for _, want := range []string{
			"intervention=grant_shield applied=0 blocked=3",
			"intervention=speed_up applied=2 blocked=3",
		} {
			if !strings.Contains(out, want) {
				t.Errorf("render lacks %q:\n%s", want, out)
			}
		}
		if strings.Contains(out, "dispatched=") {
			t.Errorf("render still names dispatched:\n%s", out)
		}
	})
	t.Run("UC-042/BR-004 applied counts", func(t *testing.T) {
		g := realGame()
		g.InterventionsByType = []gamesummary.InterventionTypeCount{{Type: "speed_up", Dispatched: 4, Applied: 4}}
		for _, a := range FromSummary(g).Interventions {
			if a.Type == "speed_up" && (a.Applied != 4 || a.Blocked != 0) {
				t.Errorf("speed_up = %+v, want applied 4 blocked 0", a)
			}
		}
	})
}
