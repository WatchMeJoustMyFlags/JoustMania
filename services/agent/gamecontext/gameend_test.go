package gamecontext

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// rootSpanFor builds the game root span the coordinator exports when a game's
// session ends (game_session.py: game.name + game.id + game.kind), ended at end.
func rootSpanFor(gameID string, end time.Time) ptrace.Traces {
	td := ptrace.NewTraces()
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("Joust Free-for-All")
	span.Attributes().PutStr(spanAttrGameName, "Joust Free-for-All")
	span.Attributes().PutStr(spanAttrGameID, gameID)
	span.Attributes().PutStr(spanAttrGameKind, "primary")
	span.Attributes().PutStr("game.outcome", "force_ended")
	if !end.IsZero() {
		span.SetEndTimestamp(pcommon.NewTimestampFromTime(end))
	}
	return td
}

// endRecorder counts OnGameEnd calls per SessionID across every partition.
type endRecorder struct {
	mu    sync.Mutex
	ends  []GameContext
	total atomic.Int32
}

func (r *endRecorder) hook(c GameContext) {
	r.mu.Lock()
	r.ends = append(r.ends, c)
	r.mu.Unlock()
	r.total.Add(1)
}

func (r *endRecorder) count() int { return int(r.total.Load()) }

func (r *endRecorder) last() GameContext {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.ends[len(r.ends)-1]
}

func newEndMux(clk *muxClock, rec *endRecorder) *Multiplexer {
	return NewMultiplexer(func(string) *Store {
		s := NewStore(5*time.Second, 15*time.Second, clk.now)
		s.OnGameEnd = rec.hook
		return s
	})
}

func hasPartition(m *Multiplexer, id string) bool {
	for _, p := range m.Partitions() {
		if p == id {
			return true
		}
	}
	return false
}

// UC: UC-041 A4 BR-005
func TestGameEnd_RootSpanEndsGameWhenZeroDropped(t *testing.T) {
	t.Run("UC-041/A4 dropped game_active=0: root span ends the game and the partition drains", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)

		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1.5))
		clk.advance(20 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		end := clk.now()

		// Retire removed the series before the 0 was exported: no 0 ever arrives.
		// The root span reaches the agent one span batch later.
		clk.advance(5 * time.Second)
		mux.ApplySpans(rootSpanFor("game-A", end))

		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
		snap := rec.last()
		if snap.SessionID != "game-A" {
			t.Errorf("end snapshot SessionID = %q, want game-A", snap.SessionID)
		}
		if snap.Session.GameActive == nil || *snap.Session.GameActive {
			t.Error("end snapshot GameActive should be false")
		}
		// Duration bracket ends at the root span's end, not at its late arrival.
		if d := snap.Session.DurationSeconds; d == nil || *d != 20 {
			t.Errorf("end snapshot duration = %v, want 20 (start -> span end)", d)
		}

		// Past the player TTL and session grace the partition is no longer pinned.
		clk.advance(20 * time.Second)
		mux.EvictStale()
		if hasPartition(mux, "game-A") {
			t.Fatal("partition game-A still pinned after root span end + grace")
		}
	})
}

// UC: UC-041 A4 BR-005
func TestGameEnd_FiresExactlyOnceWhicheverSignalFirst(t *testing.T) {
	t.Run("UC-041/BR-005 zero then root span fires once", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(10 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		mux.ApplySpans(rootSpanFor("game-A", clk.now()))
		clk.advance(time.Minute)
		mux.EvictStale()
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
	})

	t.Run("UC-041/BR-005 root span then late zero and late one fire once and never restart", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(10 * time.Second)
		mux.ApplySpans(rootSpanFor("game-A", clk.now()))
		// A late 1 (out-of-order delivery) must not start a second session for
		// the same game, and the late 0 must not end it again.
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		if snap, _ := mux.Snapshot("game-A"); snap.Session.GameActive == nil || *snap.Session.GameActive {
			t.Fatal("late game_active=1 restarted a game whose root span already ended")
		}
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		clk.advance(time.Minute)
		mux.EvictStale()
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
	})

	t.Run("UC-041/BR-005 non-root game span does not end the game", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		// player_lifecycle carries game.id + game.kind but no game.name.
		td := spanForGame("game-A", "P1")
		td.ResourceSpans().At(0).ScopeSpans().At(0).Spans().At(0).Attributes().PutStr(spanAttrGameKind, "primary")
		mux.ApplySpans(td)
		if got := rec.count(); got != 0 {
			t.Fatalf("player span ended the game (%d fires), want 0", got)
		}
		if snap, _ := mux.Snapshot("game-A"); snap.Session.GameActive == nil || !*snap.Session.GameActive {
			t.Fatal("game should still be active")
		}
	})

	t.Run("UC-041/BR-005 root span for a game the agent never saw active is a no-op", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplySpans(rootSpanFor("game-A", time.Time{}))
		if got := rec.count(); got != 0 {
			t.Fatalf("OnGameEnd fired %d times for a never-active game, want 0", got)
		}
	})
}

// UC: UC-041 A4 BR-005
func TestGameEnd_StaleGameActiveBackstop(t *testing.T) {
	t.Run("UC-041/A4 game_active stops without a 0 or root span: stale timeout ends the game", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(12 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 1)) // last datapoint
		lastSeen := 12.0

		clk.advance(gameActiveStaleAfter) // not yet past the timeout
		mux.EvictStale()
		if got := rec.count(); got != 0 {
			t.Fatalf("ended at exactly the stale timeout (%d fires), want 0", got)
		}

		clk.advance(time.Second)
		mux.EvictStale()
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times after the stale timeout, want 1", got)
		}
		if d := rec.last().Session.DurationSeconds; d == nil || *d != lastSeen {
			t.Errorf("end duration = %v, want %v (start -> last game_active datapoint)", d, lastSeen)
		}
		// Still in grace on the tick that ended it, then drained.
		if !hasPartition(mux, "game-A") {
			t.Fatal("partition removed on the same tick the game ended; grace window skipped")
		}
		clk.advance(20 * time.Second)
		mux.EvictStale()
		if hasPartition(mux, "game-A") {
			t.Fatal("partition game-A still pinned after stale end + grace")
		}
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times in total, want 1", got)
		}
	})

	t.Run("UC-041/A4 a live game exporting every second is never stale-ended", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		for i := 0; i < 120; i++ {
			mux.ApplyMetrics(gameActiveFor("game-A", 1))
			clk.advance(time.Second)
			mux.EvictStale()
		}
		if got := rec.count(); got != 0 {
			t.Fatalf("live game ended %d times, want 0", got)
		}
	})
}

// UC: UC-041 BR-005
func TestGameEnd_ConcurrentSignalsFireOnce(t *testing.T) {
	t.Run("UC-041/BR-005 concurrent zero, root span and stale eviction fire OnGameEnd once", func(t *testing.T) {
		for iter := 0; iter < 50; iter++ {
			clk := &muxClock{t: time.Unix(1000, 0)}
			rec := &endRecorder{}
			mux := newEndMux(clk, rec)
			mux.ApplyMetrics(gameActiveFor("game-A", 1))
			clk.advance(gameActiveStaleAfter + time.Second)

			var wg sync.WaitGroup
			wg.Add(3)
			go func() { defer wg.Done(); mux.ApplyMetrics(gameActiveFor("game-A", 0)) }()
			go func() { defer wg.Done(); mux.ApplySpans(rootSpanFor("game-A", clk.now())) }()
			go func() { defer wg.Done(); mux.EvictStale() }()
			wg.Wait()

			if got := rec.count(); got != 1 {
				t.Fatalf("iteration %d: OnGameEnd fired %d times, want 1", iter, got)
			}
		}
	})
}

// UC: UC-041 A4 BR-005
func TestGameEnd_StaleEndLatches(t *testing.T) {
	t.Run("UC-041/BR-005 stale end, resumed game_active=1, then 0: OnGameEnd fires once", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1.5))
		clk.advance(10 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))

		// The series goes silent past the backstop (collector restart): stale end.
		clk.advance(gameActiveStaleAfter + time.Second)
		mux.EvictStale()
		if got := rec.count(); got != 1 {
			t.Fatalf("stale end fired %d times, want 1", got)
		}

		// The series resumes: the game id is never reused, so this is the same game.
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		if snap, _ := mux.Snapshot("game-A"); snap.Session.GameActive == nil || *snap.Session.GameActive {
			t.Fatal("resumed game_active=1 restarted a game that already ended (stale)")
		}
		clk.advance(5 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		mux.ApplySpans(rootSpanFor("game-A", clk.now()))
		clk.advance(time.Minute)
		mux.EvictStale()
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
	})

	t.Run("UC-041/BR-005 zero end, then a late game_active=1 and 0: OnGameEnd fires once", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(10 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
	})

	t.Run("UC-041/BR-005 ended game's partition removed, then signals resume: no second end", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(gameActiveStaleAfter + time.Second)
		mux.EvictStale() // stale end
		clk.advance(time.Minute)
		mux.EvictStale() // grace over: partition removed
		if hasPartition(mux, "game-A") {
			t.Fatal("precondition: partition game-A should be removed")
		}

		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(5 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		if got := rec.count(); got != 1 {
			t.Fatalf("OnGameEnd fired %d times, want 1", got)
		}
	})

	t.Run("UC-041/BR-005 fallback partition: an unlabeled 0 -> 1 is a new game", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor(FallbackGameID, 1))
		mux.ApplyMetrics(gameActiveFor(FallbackGameID, 0))
		mux.ApplyMetrics(gameActiveFor(FallbackGameID, 1))
		mux.ApplyMetrics(gameActiveFor(FallbackGameID, 0))
		if got := rec.count(); got != 2 {
			t.Fatalf("fallback partition ended %d games, want 2", got)
		}
	})
}

// UC: UC-041 A4
func TestGameEnd_EndReasonAndTimelineStamp(t *testing.T) {
	endPhase := func(t *testing.T, c GameContext) time.Time {
		t.Helper()
		for _, ev := range c.Timeline {
			if ev.Kind == EventPhase && ev.Detail == "end" {
				return ev.At
			}
		}
		t.Fatal("end snapshot has no \"end\" phase event")
		return time.Time{}
	}

	t.Run("UC-041/A4 zero end: reason zero, end stamped at arrival", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(10 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		snap := rec.last()
		if snap.EndReason != EndReasonZero {
			t.Errorf("EndReason = %q, want %q", snap.EndReason, EndReasonZero)
		}
		if at := endPhase(t, snap); !at.Equal(clk.now()) {
			t.Errorf("end phase At = %v, want %v", at, clk.now())
		}
	})

	t.Run("UC-041/A4 root span end: reason root_span, end stamped at the span's end", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(20 * time.Second)
		end := clk.now()
		clk.advance(5 * time.Second) // span batch delay
		mux.ApplySpans(rootSpanFor("game-A", end))
		snap := rec.last()
		if snap.EndReason != EndReasonRootSpan {
			t.Errorf("EndReason = %q, want %q", snap.EndReason, EndReasonRootSpan)
		}
		if at := endPhase(t, snap); !at.Equal(end) {
			t.Errorf("end phase At = %v, want the span end %v (not its arrival)", at, end)
		}
	})

	t.Run("UC-041/A4 stale end: reason stale, end stamped at the last datapoint", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(1000, 0)}
		rec := &endRecorder{}
		mux := newEndMux(clk, rec)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		clk.advance(12 * time.Second)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		last := clk.now()
		clk.advance(gameActiveStaleAfter + time.Second)
		mux.EvictStale()
		snap := rec.last()
		if snap.EndReason != EndReasonStale {
			t.Errorf("EndReason = %q, want %q", snap.EndReason, EndReasonStale)
		}
		if at := endPhase(t, snap); !at.Equal(last) {
			t.Errorf("end phase At = %v, want the last datapoint %v", at, last)
		}
	})
}
