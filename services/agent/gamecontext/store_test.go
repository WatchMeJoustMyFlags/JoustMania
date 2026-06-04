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
