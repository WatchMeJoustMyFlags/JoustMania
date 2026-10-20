package gamecontext

import (
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
)

// uc041_flows_test.go covers two UC-041 alternative flows on the per-game
// multiplexer: a later signal recreates an evicted player (A2 step 2), and a
// lost telemetry batch is restored by the next export (A6).

// UC: UC-041 A2
func TestMux_LaterSignalRecreatesEvictedPlayer(t *testing.T) {
	t.Run("UC-041/A2 a player evicted past its TTL comes back fresh on its next signal", func(t *testing.T) {
		clk := &muxClock{t: time.Unix(7000, 0)}
		mux := newTestMux(clk, 5*time.Second, 15*time.Second, nil, nil)
		mux.ApplyMetrics(gameActiveFor("game-A", 1))
		mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1.0))
		mux.ApplyMetrics(playerIntensityFor("P2", "game-A", 1.0))

		// P1 goes silent past the 5 s TTL; P2 keeps sending.
		clk.advance(6 * time.Second)
		mux.ApplyMetrics(playerIntensityFor("P2", "game-A", 1.0))
		mux.EvictStale()
		snap, ok := mux.Snapshot("game-A")
		if !ok {
			t.Fatal("the active game's partition must survive a player eviction")
		}
		if _, ok := snap.Players["P1"]; ok {
			t.Fatal("P1 must be evicted after its TTL")
		}
		if _, ok := snap.Players["P2"]; !ok {
			t.Fatal("P2 kept sending and must stay")
		}

		// P1's next signal recreates it, fresh and with the new value only.
		clk.advance(time.Second)
		mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 2.5))
		snap, _ = mux.Snapshot("game-A")
		p, ok := snap.Players["P1"]
		if !ok {
			t.Fatal("a later signal must recreate the evicted player")
		}
		if p.MovementIntensity == nil || *p.MovementIntensity != 2.5 {
			t.Fatalf("recreated P1 intensity = %v, want 2.5", p.MovementIntensity)
		}
		if !p.LastUpdate.Equal(clk.now()) {
			t.Fatalf("recreated P1 LastUpdate = %v, want now %v (fresh)", p.LastUpdate, clk.now())
		}
		// Fresh: it survives the next eviction tick.
		mux.EvictStale()
		if snap, _ = mux.Snapshot("game-A"); snap.Players["P1"] == nil {
			t.Fatal("the recreated player is fresh and must survive the next eviction tick")
		}
	})
}

// eliminationOrderFor builds a game_player_elimination_order datapoint.
func eliminationOrderFor(serial, gameID string, order float64) pmetric.Metrics {
	md := pmetric.NewMetrics()
	addGauge(md, metricEliminationOrder, order, map[string]string{attrSerial: serial, attrGameID: gameID})
	return md
}

// UC: UC-041 A6
func TestMux_LostBatchRestoredByNextExport(t *testing.T) {
	const interval = 100 * time.Millisecond // the coordinator's metrics_export_interval_ms
	clk := &muxClock{t: time.Unix(9000, 0)}
	var (
		mu   sync.Mutex
		ends []GameContext
	)
	mux := NewMultiplexer(func(string) *Store {
		s := NewStore(time.Hour, time.Hour, clk.now)
		s.OnGameEnd = func(c GameContext) {
			mu.Lock()
			ends = append(ends, c)
			mu.Unlock()
		}
		return s
	})

	t.Run("UC-041/A6 a lost game_active 1 starts the game one interval later", func(t *testing.T) {
		// The batch carrying the first game_active 1 is dropped by the collector:
		// the agent never sees it, so nothing is applied for it.
		if _, ok := mux.Snapshot("game-A"); ok {
			t.Fatal("no partition may exist before any batch for game-A arrives")
		}
		clk.advance(interval)
		mux.ApplyMetrics(gameActiveFor("game-A", 1)) // the next export
		snap, ok := mux.Snapshot("game-A")
		if !ok || snap.Session.GameActive == nil || !*snap.Session.GameActive {
			t.Fatalf("game-A after the re-export = %+v, want active", snap.Session)
		}
		if !snap.Session.StartedAt.Equal(clk.now()) {
			t.Fatalf("StartedAt = %v, want the re-export time %v", snap.Session.StartedAt, clk.now())
		}
	})

	t.Run("UC-041/A6 a lost player_death leaves the elimination order to the gauge", func(t *testing.T) {
		mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1))
		mux.ApplyMetrics(playerIntensityFor("P2", "game-A", 1))
		// The player_death span event for P1 is lost; the gauge batch is not.
		clk.advance(interval)
		mux.ApplyMetrics(eliminationOrderFor("P1", "game-A", 1))
		snap, _ := mux.Snapshot("game-A")
		if seq := snap.Session.EliminationSequence; len(seq) != 1 || seq[0] != "P1" {
			t.Fatalf("EliminationSequence = %v, want [P1] from the gauge", seq)
		}
	})

	t.Run("UC-041/A6 a lost root span leaves the end to game_active 0", func(t *testing.T) {
		// The root span never arrives; the first game_active 0 batch is lost too
		// and the next export carries it.
		clk.advance(interval)
		mux.ApplyMetrics(gameActiveFor("game-A", 0))
		clk.advance(interval)
		mux.ApplyMetrics(gameActiveFor("game-A", 0)) // a repeated export ends nothing twice
		mu.Lock()
		defer mu.Unlock()
		if len(ends) != 1 {
			t.Fatalf("game ends = %d, want exactly 1", len(ends))
		}
		if ends[0].SessionID != "game-A" || ends[0].EndReason != EndReasonZero {
			t.Fatalf("end = (%q, %q), want (game-A, %q)", ends[0].SessionID, ends[0].EndReason, EndReasonZero)
		}
	})
}
