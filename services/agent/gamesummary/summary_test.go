package gamesummary

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// base is the +0s anchor every test offset is measured from. A fixed instant keeps
// the tests deterministic (BuildSummary is clock-free; only GeneratedAt is injected).
var base = time.Date(2026, 6, 12, 10, 0, 0, 0, time.UTC)

func at(sec int) time.Time { return base.Add(time.Duration(sec) * time.Second) }

func fptr(v float64) *float64 { return &v }
func iptr(v int) *int         { return &v }

// representativeGame drives a Store through a full game with an injected clock and
// returns the game-end snapshot: 3 players join, the active count and intensity
// move (engagement trend), two players are eliminated, and the game ends. The
// survivor (CC:33) outlasts the field. Two interventions are recorded on the
// timeline (one dispatched, one blocked) via the #916 seam.
func representativeGame(t *testing.T) gamecontext.GameContext {
	t.Helper()
	clock := base
	s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })

	var snap gamecontext.GameContext
	s.OnGameEnd = func(c gamecontext.GameContext) { snap = c }

	s.SetGameKind("real")
	s.SetGameMode("joust")
	s.SetGameActive(true) // phase: start at +0s (assigns synthetic session id)
	// Adopt the real game_id AFTER session start: SetGameActive(true) assigns a
	// synthetic "session-N" on the false->true transition, so a real game_id label
	// is adopted afterward (mirrors the receiver's order in production).
	s.AdoptSessionID("game_abc123def456")

	// Players join + initial energy.
	s.SetActivePlayerCount(3)
	s.SetPlayerIntensity("AA:11", 0.8)
	s.SetPlayerIntensity("BB:22", 0.7)
	s.SetPlayerIntensity("CC:33", 0.9)
	s.SetPlayerSkill("CC:33", 0.95, true)

	clock = at(20)
	s.SetPlayerIntensity("AA:11", 0.4) // energy fades -> state delta
	// First intervention fires around +20s (dispatched).
	s.AppendInterventionEvent("music_tempo_override", "", false)

	clock = at(25)
	s.SetEliminationOrder("AA:11", 1) // first elimination at +25s
	s.SetActivePlayerCount(2)

	clock = at(30)
	s.SetPlayerIntensity("BB:22", 0.95) // post-intervention energy bump

	clock = at(50)
	// Second intervention (blocked) at +50s.
	s.AppendInterventionEvent("eliminate_player", "BB:22", true)
	s.SetEliminationOrder("BB:22", 2) // second elimination at +50s
	s.SetActivePlayerCount(1)

	clock = at(60)
	s.SetSessionDuration(60)
	s.SetGameActive(false) // phase: end at +60s -> OnGameEnd fires

	if snap.SessionID == "" {
		t.Fatal("OnGameEnd did not fire / empty snapshot")
	}
	return snap
}

func TestBuildSummary_TopLevel(t *testing.T) {
	snap := representativeGame(t)
	now := at(61)
	got := BuildSummary(snap, BuildOptions{Now: now})

	if got.SchemaVersion != SchemaVersion {
		t.Errorf("SchemaVersion = %d, want %d", got.SchemaVersion, SchemaVersion)
	}
	if got.GameID != "game_abc123def456" {
		t.Errorf("GameID = %q", got.GameID)
	}
	if got.GameKind != "real" {
		t.Errorf("GameKind = %q, want real", got.GameKind)
	}
	if got.GameMode != "joust" {
		t.Errorf("GameMode = %q, want joust", got.GameMode)
	}
	if !got.GeneratedAt.Equal(now) {
		t.Errorf("GeneratedAt = %v, want %v", got.GeneratedAt, now)
	}
	if got.DurationSeconds == nil || *got.DurationSeconds != 60 {
		t.Errorf("DurationSeconds = %v, want 60", got.DurationSeconds)
	}
	if got.PlayerCount != 3 {
		t.Errorf("PlayerCount = %d, want 3", got.PlayerCount)
	}
	if got.EliminationCount != 2 {
		t.Errorf("EliminationCount = %d, want 2", got.EliminationCount)
	}
	if got.InterventionCount != 2 {
		t.Errorf("InterventionCount = %d, want 2", got.InterventionCount)
	}
}

func TestBuildSummary_Determinism(t *testing.T) {
	snap := representativeGame(t)
	now := at(61)
	a := BuildSummary(snap, BuildOptions{Now: now})
	b := BuildSummary(snap, BuildOptions{Now: now})
	if a.GameID != b.GameID || len(a.PlayerArcs) != len(b.PlayerArcs) ||
		len(a.EngagementTrend) != len(b.EngagementTrend) ||
		a.Dominance != b.Dominance {
		t.Error("BuildSummary is not deterministic over the same snapshot")
	}
	// Player arcs must be serial-sorted for a byte-stable file.
	for i := 1; i < len(a.PlayerArcs); i++ {
		if a.PlayerArcs[i-1].Serial > a.PlayerArcs[i].Serial {
			t.Errorf("PlayerArcs not serial-sorted: %v", a.PlayerArcs)
		}
	}
}

func TestBuildSummary_EliminationSpread(t *testing.T) {
	got := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	if len(got.EliminationSpread) != 2 {
		t.Fatalf("EliminationSpread len = %d, want 2", len(got.EliminationSpread))
	}
	first, second := got.EliminationSpread[0], got.EliminationSpread[1]
	if first.Serial != "AA:11" || first.Order != 1 || first.OffsetSeconds != 25 {
		t.Errorf("first elimination = %+v, want AA:11 order 1 @25s", first)
	}
	if second.Serial != "BB:22" || second.Order != 2 || second.OffsetSeconds != 50 {
		t.Errorf("second elimination = %+v, want BB:22 order 2 @50s", second)
	}
}

func TestBuildSummary_PlayerArcs(t *testing.T) {
	got := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	arcs := make(map[string]PlayerArc, len(got.PlayerArcs))
	for _, a := range got.PlayerArcs {
		arcs[a.Serial] = a
	}

	aa := arcs["AA:11"]
	if !aa.Eliminated || aa.EliminationOrder != 1 {
		t.Errorf("AA:11 arc = %+v, want eliminated order 1", aa)
	}
	if aa.EliminationOffsetSeconds == nil || *aa.EliminationOffsetSeconds != 25 {
		t.Errorf("AA:11 elimination offset = %v, want 25", aa.EliminationOffsetSeconds)
	}
	if aa.SurvivalSeconds == nil || *aa.SurvivalSeconds != 25 {
		t.Errorf("AA:11 survival = %v, want 25", aa.SurvivalSeconds)
	}

	cc := arcs["CC:33"]
	if cc.Eliminated {
		t.Errorf("CC:33 must be the survivor, got eliminated: %+v", cc)
	}
	// Survivor survival == full game span (anchor..last event = 0..60).
	if cc.SurvivalSeconds == nil || *cc.SurvivalSeconds != 60 {
		t.Errorf("CC:33 survival = %v, want 60 (full game)", cc.SurvivalSeconds)
	}
	if cc.FinalSkill == nil || *cc.FinalSkill != 0.95 {
		t.Errorf("CC:33 final skill = %v, want 0.95", cc.FinalSkill)
	}
}

func TestBuildSummary_EngagementTrend(t *testing.T) {
	got := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	if len(got.EngagementTrend) == 0 {
		t.Fatal("EngagementTrend is empty; expected state deltas from the timeline")
	}
	// Trend must be oldest-first (non-decreasing offsets).
	for i := 1; i < len(got.EngagementTrend); i++ {
		if got.EngagementTrend[i-1].OffsetSeconds > got.EngagementTrend[i].OffsetSeconds {
			t.Errorf("EngagementTrend not oldest-first: %+v", got.EngagementTrend)
		}
	}
	// The active count fell 3 -> 2 -> 1 over the game; the last point must show 1.
	last := got.EngagementTrend[len(got.EngagementTrend)-1]
	if last.ActivePlayers == nil || *last.ActivePlayers != 1 {
		t.Errorf("last engagement active_players = %v, want 1", last.ActivePlayers)
	}
}

func TestBuildSummary_Dominance(t *testing.T) {
	got := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	// CC:33 survived 60s; the eliminated mean is (25+50)/2 = 37.5; 60/37.5 = 1.6,
	// below the 2x ratio -> NOT dominated, but the heuristic + margin are recorded.
	if got.Dominance.Dominated {
		t.Errorf("expected not dominated (margin 1.6x < 2x), got %+v", got.Dominance)
	}
	if got.Dominance.Heuristic == "" {
		t.Error("Dominance.Heuristic must always be set")
	}
	if got.Dominance.Margin < 1.59 || got.Dominance.Margin > 1.61 {
		t.Errorf("Dominance.Margin = %v, want ~1.6", got.Dominance.Margin)
	}
}

// TestBuildSummary_DominanceDetected: a survivor far outlasting the field flips the
// dominated flag and names the player.
func TestBuildSummary_DominanceDetected(t *testing.T) {
	clock := base
	s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return clock })
	var snap gamecontext.GameContext
	s.OnGameEnd = func(c gamecontext.GameContext) { snap = c }

	s.SetGameKind("shadow")
	s.SetGameActive(true)
	s.AdoptSessionID("game_dom")
	s.SetActivePlayerCount(3)
	s.SetPlayerIntensity("WIN:01", 0.9)

	clock = at(5)
	s.SetEliminationOrder("LOS:01", 1) // out fast at +5s

	clock = at(8)
	s.SetEliminationOrder("LOS:02", 2) // out fast at +8s

	clock = at(90)
	s.SetActivePlayerCount(1)
	s.SetGameActive(false) // survivor WIN:01 lasted 90s

	got := BuildSummary(snap, BuildOptions{Now: at(91)})
	if !got.Dominance.Dominated {
		t.Fatalf("expected dominated, got %+v", got.Dominance)
	}
	if got.Dominance.Serial != "WIN:01" {
		t.Errorf("dominant serial = %q, want WIN:01", got.Dominance.Serial)
	}
}

func TestBuildSummary_InterventionOutcomes(t *testing.T) {
	got := BuildSummary(representativeGame(t), BuildOptions{Now: at(61)})
	if len(got.Interventions) != 2 {
		t.Fatalf("Interventions len = %d, want 2", len(got.Interventions))
	}
	byName := make(map[string]InterventionOutcome, 2)
	for _, iv := range got.Interventions {
		byName[iv.Intervention] = iv
	}

	tempo := byName["music_tempo_override"]
	if !tempo.Dispatched {
		t.Errorf("music_tempo_override should be dispatched: %+v", tempo)
	}
	if tempo.OffsetSeconds != 20 {
		t.Errorf("music_tempo_override offset = %v, want 20", tempo.OffsetSeconds)
	}

	elim := byName["eliminate_player"]
	if elim.Dispatched {
		t.Errorf("eliminate_player should be blocked: %+v", elim)
	}
	if elim.TargetSerial != "BB:22" {
		t.Errorf("eliminate_player target = %q, want BB:22", elim.TargetSerial)
	}
}

// TestBuildSummary_InterventionEffect: the best-effort before/after delta is
// computed when the timeline has state-deltas on both sides of an intervention.
func TestBuildSummary_InterventionEffect(t *testing.T) {
	// Craft a timeline directly: intensity rises after the intervention.
	tl := []gamecontext.TimelineEvent{
		{At: at(0), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.40)},
		{At: at(10), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.42)},
		{At: at(15), Kind: gamecontext.EventIntervention, Detail: "music_tempo_override"},
		{At: at(20), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.80)},
		{At: at(25), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(2), MeanIntensity: fptr(0.82)},
	}
	gc := gamecontext.GameContext{
		SessionID: "game_eff",
		Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
		Timeline:  tl,
	}
	got := BuildSummary(gc, BuildOptions{Now: at(30)})
	if len(got.Interventions) != 1 {
		t.Fatalf("want 1 intervention, got %d", len(got.Interventions))
	}
	eff := got.Interventions[0].Effect
	if eff == nil || eff.MeanIntensityDelta == nil {
		t.Fatalf("expected a mean-intensity effect delta, got %+v", eff)
	}
	// before mean = (0.40+0.42)/2 = 0.41; after mean = (0.80+0.82)/2 = 0.81; delta +0.40.
	if *eff.MeanIntensityDelta < 0.39 || *eff.MeanIntensityDelta > 0.41 {
		t.Errorf("MeanIntensityDelta = %v, want ~0.40", *eff.MeanIntensityDelta)
	}
}

// TestBuildSummary_EmptyTimeline: a game that produced no narrative still yields a
// well-formed, non-panicking summary (no eliminations, no trend).
func TestBuildSummary_EmptyTimeline(t *testing.T) {
	gc := gamecontext.GameContext{
		SessionID: "game_empty",
		GameKind:  "real",
		Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
		Players:   map[string]*gamecontext.PlayerSignals{"AA:11": {Serial: "AA:11"}},
	}
	got := BuildSummary(gc, BuildOptions{Now: at(1)})
	if got.PlayerCount != 1 || got.EliminationCount != 0 || len(got.EngagementTrend) != 0 {
		t.Errorf("empty-timeline summary = %+v", got)
	}
	if got.Dominance.Dominated {
		t.Error("empty game cannot be dominated")
	}
}

func bptr(b bool) *bool { return &b }
