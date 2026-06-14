package gamecontext

import (
	"sync"
	"testing"
	"time"
)

// clock is a mutable injected time source for deterministic tests.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func TestStore_TTLEviction(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(5*time.Second, time.Hour, clk.now)

	s.SetPlayerIntensity("A", 1.0)
	clk.advance(6 * time.Second)
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; ok {
		t.Fatal("player should be evicted past TTL")
	}
}

func TestStore_DisconnectEviction(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetPlayerConnected("A", true)
	s.SetPlayerConnected("A", false) // marks disconnected
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; ok {
		t.Fatal("disconnected player should be evicted")
	}
}

func TestStore_EvictedDeathBaselineCleared(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(5*time.Second, time.Hour, clk.now)

	s.SetPlayerIntensity("A", 1.0)
	s.RecordDeathTotal("A", 3) // baseline at 3

	clk.advance(6 * time.Second)
	s.EvictStale() // player + death baseline cleared

	// A new baseline observation; an "increase" relative to the OLD baseline
	// must NOT fake an elimination because the baseline was cleared.
	s.RecordDeathTotal("A", 4) // treated as fresh baseline
	if seq := s.Snapshot().Session.EliminationSequence; len(seq) != 0 {
		t.Fatalf("elimination seq = %v, want empty (baseline was cleared)", seq)
	}
}

func TestStore_SessionGraceClearsSessionKeepsPlayers(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, 15*time.Second, clk.now)

	s.SetGameActive(true)
	s.SetPlayerIntensity("A", 1.0)
	s.SetGameActive(false) // opens grace window

	clk.advance(16 * time.Second)
	s.EvictStale()

	snap := s.Snapshot()
	if snap.SessionID != "" {
		t.Fatalf("SessionID = %q, want empty after grace", snap.SessionID)
	}
	if snap.Session.GameActive == nil || *snap.Session.GameActive {
		t.Fatal("GameActive should be false after grace reset")
	}
	if _, ok := snap.Players["A"]; !ok {
		t.Fatal("players should persist across session reset (lobby continues)")
	}
}

// TestStore_LiveTTLSourceHotReloads verifies the live player-TTL source (#927):
// EvictStale reads lifecycle.player_ttl_seconds from the source AT EVICTION TIME,
// so shortening it mid-session evicts a player that the construction-time TTL
// would have retained — no restart. Driven with the fake clock and a settable
// source (the shared LifecycleHolder.PlayerTTL in production), no flagd.
func TestStore_LiveTTLSourceHotReloads(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	// Construct with a LONG static TTL (1h) so only the live source can evict.
	s := NewStore(time.Hour, time.Hour, clk.now)
	playerTTL := time.Hour
	s.SetTTLSources(func() time.Duration { return playerTTL }, nil)

	s.SetPlayerIntensity("A", 1.0)
	clk.advance(10 * time.Second)

	// Under the live 1h TTL the player is retained (matches the static value).
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; !ok {
		t.Fatal("player evicted under 1h live TTL; should be retained")
	}

	// Config-change: shorten the live TTL to 5s. The same 10s-silent player now
	// exceeds it, so the NEXT EvictStale removes it — proving the new value took
	// effect with no restart and the static 1h field was overridden.
	playerTTL = 5 * time.Second
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; ok {
		t.Fatal("player should be evicted after live TTL shortened to 5s (#927)")
	}
}

// TestStore_LiveSessionGraceSourceHotReloads verifies the live session-grace
// source (#927): EvictStale reads lifecycle.session_grace_seconds live, so
// shortening it mid-session resets the session sooner than the construction value.
func TestStore_LiveSessionGraceSourceHotReloads(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now) // long static grace
	grace := time.Hour
	s.SetTTLSources(nil, func() time.Duration { return grace })

	s.SetGameActive(true)
	s.SetGameActive(false) // opens grace window
	clk.advance(20 * time.Second)

	// Under the 1h live grace, the session is still inside its window.
	s.EvictStale()
	if s.Snapshot().SessionID == "" {
		t.Fatal("session reset under 1h live grace; should still be in grace window")
	}

	// Shorten live grace to 5s: the 20s-ended session now exceeds it and resets.
	grace = 5 * time.Second
	s.EvictStale()
	if id := s.Snapshot().SessionID; id != "" {
		t.Fatalf("SessionID = %q, want empty after live grace shortened to 5s (#927)", id)
	}
}

// TestStore_NilTTLSourceUsesStaticValue verifies that with no live source wired
// (all existing call sites / tests) EvictStale falls back to the construction-time
// TTL exactly as before #927 — the change is purely additive.
func TestStore_NilTTLSourceUsesStaticValue(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(5*time.Second, time.Hour, clk.now) // no SetTTLSources call
	s.SetPlayerIntensity("A", 1.0)
	clk.advance(6 * time.Second)
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; ok {
		t.Fatal("player should be evicted past the static 5s TTL with no live source")
	}
}

// TestStore_NonPositiveTTLSourceFallsBackToStatic verifies a source returning a
// non-positive duration is ignored in favor of the static TTL, so a transient bad
// flag read can never collapse the TTL to zero and evict everyone (#927).
func TestStore_NonPositiveTTLSourceFallsBackToStatic(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now) // long static TTL
	s.SetTTLSources(func() time.Duration { return 0 }, nil)
	s.SetPlayerIntensity("A", 1.0)
	clk.advance(10 * time.Second)
	s.EvictStale()
	if _, ok := s.Snapshot().Players["A"]; !ok {
		t.Fatal("non-positive live TTL must fall back to the static 1h TTL; player wrongly evicted")
	}
}

func TestStore_SnapshotIsolation(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetPlayerIntensity("A", 1.0)
	s.RecordElimination("A")
	snap := s.Snapshot()

	// Mutate the store after snapshotting.
	s.SetPlayerIntensity("A", 99.0)
	s.RecordElimination("B")

	if got := *snap.Players["A"].MovementIntensity; got != 1.0 {
		t.Fatalf("snapshot player mutated: got %v, want 1.0", got)
	}
	if len(snap.Session.EliminationSequence) != 1 {
		t.Fatalf("snapshot elimination seq mutated: %v", snap.Session.EliminationSequence)
	}
}

// TestStore_OnGameEnd_FiresOnceWithPreResetState: the hook fires exactly once on
// the GameActive true->false transition, with the elimination sequence and
// SessionID still intact (the snapshot is taken before any session reset).
func TestStore_OnGameEnd_FiresOnceWithPreResetState(t *testing.T) {
	s := NewStore(time.Hour, time.Hour, nil)
	var got []GameContext
	s.OnGameEnd = func(c GameContext) { got = append(got, c) }

	s.SetGameActive(true) // false->true: no fire
	s.SetEliminationOrder("BB:22", 0)
	s.SetEliminationOrder("CC:33", 1)
	sessionID := s.Snapshot().SessionID

	s.SetGameActive(false) // true->false: fire once

	if len(got) != 1 {
		t.Fatalf("OnGameEnd fired %d times, want 1", len(got))
	}
	end := got[0]
	if end.SessionID != sessionID || end.SessionID == "" {
		t.Errorf("snapshot SessionID = %q, want pre-reset %q", end.SessionID, sessionID)
	}
	wantSeq := []string{"BB:22", "CC:33"}
	if len(end.Session.EliminationSequence) != len(wantSeq) {
		t.Fatalf("elimination sequence = %v, want %v", end.Session.EliminationSequence, wantSeq)
	}
	for i, s := range wantSeq {
		if end.Session.EliminationSequence[i] != s {
			t.Errorf("elimination[%d] = %q, want %q", i, end.Session.EliminationSequence[i], s)
		}
	}
	// The transition is also visible in the snapshot: GameActive is already false.
	if end.Session.GameActive == nil || *end.Session.GameActive {
		t.Error("snapshot GameActive should be false (post-transition, pre-reset)")
	}
}

// TestStore_OnGameEnd_NotOnStartOrRepeatedFalse: the hook does not fire on
// false->true (game start) nor on a repeated false->false call.
func TestStore_OnGameEnd_NotOnStartOrRepeatedFalse(t *testing.T) {
	s := NewStore(time.Hour, time.Hour, nil)
	fires := 0
	s.OnGameEnd = func(GameContext) { fires++ }

	s.SetGameActive(true) // false->true
	if fires != 0 {
		t.Fatalf("fired on game start, want 0 (got %d)", fires)
	}
	s.SetGameActive(false) // true->false: 1
	s.SetGameActive(false) // false->false: no additional fire
	if fires != 1 {
		t.Fatalf("OnGameEnd fired %d times, want 1 (no repeated-false fire)", fires)
	}
}

// TestStore_OnGameEnd_NilHookSafe: a nil hook is the disabled default and must
// not panic on a true->false transition.
func TestStore_OnGameEnd_NilHookSafe(t *testing.T) {
	s := NewStore(time.Hour, time.Hour, nil)
	s.SetGameActive(true)
	s.SetGameActive(false) // must not panic with OnGameEnd == nil
}

func TestStore_ConcurrencySmoke(t *testing.T) {
	s := NewStore(time.Hour, time.Hour, nil)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				s.SetPlayerIntensity("A", float64(j))
				s.SetPlayerConnected("B", j%2 == 0)
				s.SetGameActive(j%3 == 0)
				s.RecordDeathTotal("C", float64(j))
				_ = s.Snapshot()
				s.EvictStale()
			}
		}(i)
	}
	wg.Wait()
}

// --- #916 rolling-narrative Store integration ---

// timelineKinds extracts the kinds of a snapshot's timeline, in order.
func timelineKinds(snap GameContext) []TimelineEventKind {
	out := make([]TimelineEventKind, len(snap.Timeline))
	for i, e := range snap.Timeline {
		out[i] = e.Kind
	}
	return out
}

func hasKind(snap GameContext, k TimelineEventKind) bool {
	for _, e := range snap.Timeline {
		if e.Kind == k {
			return true
		}
	}
	return false
}

// TestStore_TimelineAccumulates checks that the Store records a phase-start, a
// state delta, and an elimination into the rolling narrative, in observation
// order, on the partition's snapshot (#916).
func TestStore_TimelineAccumulates(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetGameActive(true) // phase: start
	clk.advance(time.Second)
	s.SetActivePlayerCount(3) // state delta
	clk.advance(time.Second)
	s.SetActivePlayerCount(2) // changed -> another state delta
	clk.advance(time.Second)
	s.RecordElimination("A") // elimination

	got := timelineKinds(s.Snapshot())
	want := []TimelineEventKind{EventPhase, EventStateDelta, EventStateDelta, EventElimination}
	if len(got) != len(want) {
		t.Fatalf("timeline kinds = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("event[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// TestStore_TimelineStateDeltaDeduped verifies a repeated identical aggregate
// does NOT churn the ring: only CHANGES are recorded (#916).
func TestStore_TimelineStateDeltaDeduped(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetActivePlayerCount(3)
	s.SetActivePlayerCount(3) // identical -> no new entry
	s.SetActivePlayerCount(3)
	if n := len(s.Snapshot().Timeline); n != 1 {
		t.Fatalf("timeline len = %d, want 1 (identical deltas deduped)", n)
	}
	s.SetActivePlayerCount(2) // change -> one more
	if n := len(s.Snapshot().Timeline); n != 2 {
		t.Fatalf("timeline len = %d, want 2 after a real change", n)
	}
}

// TestStore_TimelinePhaseStartEnd checks both phase transitions are recorded,
// and that a restart clears the prior game's narrative (#916).
func TestStore_TimelinePhaseStartEnd(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetGameActive(true)
	s.RecordElimination("A")
	s.SetGameActive(false) // phase: end

	end := s.Snapshot().Timeline
	if len(end) == 0 || end[len(end)-1].Kind != EventPhase || end[len(end)-1].Detail != "end" {
		t.Fatalf("last event = %+v, want a phase:end", end[len(end)-1])
	}

	// A restart resets the narrative: the prior elimination must be gone.
	s.SetGameActive(true)
	restarted := s.Snapshot()
	if hasKind(restarted, EventElimination) {
		t.Error("restart did not clear the prior game's elimination from the timeline")
	}
	if k := timelineKinds(restarted); len(k) != 1 || k[0] != EventPhase {
		t.Errorf("after restart timeline = %v, want a single phase:start", k)
	}
}

// TestStore_TimelineEliminationNoDoubleNarration verifies a serial is narrated as
// eliminated exactly ONCE regardless of which order the death/span path
// (RecordElimination) and the preferred order source (SetEliminationOrder) report
// it. The two paths dedupe on different stores (EliminationSequence vs elimOrder),
// so without the cross-store guard the order source emits a second elimination
// line for a serial the death/span path already narrated (#916).
func TestStore_TimelineEliminationNoDoubleNarration(t *testing.T) {
	count := func(s *Store) int {
		n := 0
		for _, e := range s.Snapshot().Timeline {
			if e.Kind == EventElimination && e.Serial == "A" {
				n++
			}
		}
		return n
	}

	// The buggy direction: death/span narrates + adds to EliminationSequence, then
	// the order source reports the same serial (elimOrder did not yet know it).
	t.Run("span_then_order", func(t *testing.T) {
		clk := &clock{t: time.Unix(0, 0)}
		s := NewStore(time.Hour, time.Hour, clk.now)
		s.RecordElimination("A")
		clk.advance(time.Second)
		s.SetEliminationOrder("A", 1)
		if n := count(s); n != 1 {
			t.Fatalf("elimination lines for A = %d, want 1 (no double-narration)", n)
		}
	})

	// The reverse direction was already safe (the rebuilt sequence dedupes the later
	// append); assert it so the invariant is documented from both sides.
	t.Run("order_then_span", func(t *testing.T) {
		clk := &clock{t: time.Unix(0, 0)}
		s := NewStore(time.Hour, time.Hour, clk.now)
		s.SetEliminationOrder("A", 1)
		clk.advance(time.Second)
		s.RecordElimination("A")
		if n := count(s); n != 1 {
			t.Fatalf("elimination lines for A = %d, want 1 (no double-narration)", n)
		}
	})
}

// TestStore_TimelineEvictedOnGrace verifies the narrative is cleared with the
// rest of the session-scoped state on grace exit (#916 eviction).
func TestStore_TimelineEvictedOnGrace(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, 15*time.Second, clk.now)

	s.SetGameActive(true)
	s.SetActivePlayerCount(3)
	s.SetGameActive(false)

	clk.advance(16 * time.Second)
	s.EvictStale()

	if got := s.Snapshot().Timeline; got != nil {
		t.Fatalf("timeline = %v, want nil after grace eviction", got)
	}
}

// TestStore_TimelineCapBounded drives more than capacity of distinct state
// deltas and confirms the snapshot's timeline never exceeds timelineCap (#916).
func TestStore_TimelineCapBounded(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.SetGameActive(true) // 1 phase event
	for i := 0; i < timelineCap*2; i++ {
		s.SetActivePlayerCount(i) // each distinct -> a state delta
	}
	if n := len(s.Snapshot().Timeline); n != timelineCap {
		t.Fatalf("timeline len = %d, want capped at %d", n, timelineCap)
	}
}

// TestStore_AppendInterventionEvent covers the deferred-seam method: a
// dispatched and a blocked intervention land in the narrative (#916).
func TestStore_AppendInterventionEvent(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	s.AppendInterventionEvent("set_music_tempo", "", false)
	s.AppendInterventionEvent("grant_shield", "BB:22", true)

	tl := s.Snapshot().Timeline
	if len(tl) != 2 {
		t.Fatalf("timeline len = %d, want 2", len(tl))
	}
	if tl[0].Kind != EventIntervention || tl[0].Detail != "set_music_tempo" || tl[0].Blocked {
		t.Errorf("event[0] = %+v, want dispatched set_music_tempo", tl[0])
	}
	if tl[1].Serial != "BB:22" || !tl[1].Blocked {
		t.Errorf("event[1] = %+v, want blocked grant_shield -> BB:22", tl[1])
	}
}

// TestStore_SkillLevelSurvivesToConclusionSnapshot is the #1015 ground-truth proof:
// driving the realistic ingest path (per-player skill/movement setters while alive,
// then game_player_alive=0 on elimination, then game_active=0) leaves each
// eliminated player's SkillLevel intact in the OnGameEnd snapshot. This is the fact
// the corrected balanced fitness rests on: balanced's skill-gap sub-check reads
// these retained SkillLevels directly (not Active-gated), so a concluded game with
// >=2 players carrying skill_level folds NON-ZERO with no recovery step — refuting
// the original "balanced folds 0 at conclusion" premise. (The Store never wipes
// Players or their SkillLevel on game end; the snapshot is taken before any TTL
// eviction, in SetGameActive's true->false transition.)
func TestStore_SkillLevelSurvivesToConclusionSnapshot(t *testing.T) {
	clk := &clock{t: time.Unix(1000, 0)}
	s := NewStore(time.Hour, time.Hour, clk.now)

	var endSnap *GameContext
	s.OnGameEnd = func(c GameContext) { cp := c; endSnap = &cp }

	s.SetGameActive(true)
	for _, pl := range []struct {
		serial string
		skill  float64
	}{{"a", 0.45}, {"b", 0.55}} {
		s.SetPlayerAlive(pl.serial, true)
		s.SetPlayerSkill(pl.serial, pl.skill, true) // preferred source: game_player_skill_level
	}
	// Both eliminated (game_player_alive=0) before the game ends.
	s.SetPlayerAlive("a", false)
	s.SetPlayerAlive("b", false)
	s.SetGameActive(false) // fires OnGameEnd with the pre-reset snapshot

	if endSnap == nil {
		t.Fatal("OnGameEnd never fired")
	}
	withSkill := 0
	for serial, p := range endSnap.Players {
		if p.Active == nil || *p.Active {
			t.Errorf("player %s expected Active=false at conclusion, got %v", serial, p.Active)
		}
		if p.SkillLevel != nil {
			withSkill++
		}
	}
	if withSkill < 2 {
		t.Fatalf("only %d eliminated players retained SkillLevel at conclusion; balanced skill-gap would not compute", withSkill)
	}
}
