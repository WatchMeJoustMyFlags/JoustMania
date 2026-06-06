package gamecontext

import (
	"sort"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// muxClock is a mutable injected time source shared by every partition the test
// factory builds, so advancing it drives eviction across all partitions at once.
type muxClock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *muxClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *muxClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

// newTestMux builds a Multiplexer whose partitions share clk and the given TTL /
// grace. ends, when non-nil, records every partition's OnGameEnd SessionID so the
// per-partition retro wiring can be asserted.
func newTestMux(clk *muxClock, playerTTL, grace time.Duration, ends *[]string, endsMu *sync.Mutex) *Multiplexer {
	return NewMultiplexer(func(string) *Store {
		s := NewStore(playerTTL, grace, clk.now)
		if ends != nil {
			s.OnGameEnd = func(c GameContext) {
				endsMu.Lock()
				*ends = append(*ends, c.SessionID)
				endsMu.Unlock()
			}
		}
		return s
	})
}

// addGauge appends a single-datapoint gauge metric to md with the given attrs.
func addGauge(md pmetric.Metrics, name string, value float64, attrs map[string]string) {
	m := md.ResourceMetrics().AppendEmpty().ScopeMetrics().AppendEmpty().Metrics().AppendEmpty()
	m.SetName(name)
	dp := m.SetEmptyGauge().DataPoints().AppendEmpty()
	dp.SetDoubleValue(value)
	for k, v := range attrs {
		dp.Attributes().PutStr(k, v)
	}
}

// playerIntensityFor builds a metric batch with one accel-magnitude datapoint
// carrying the given serial + game_id (the live per-player routing path).
func playerIntensityFor(serial, gameID string, value float64) pmetric.Metrics {
	md := pmetric.NewMetrics()
	addGauge(md, metricAccelMagnitude, value, map[string]string{attrSerial: serial, attrGameID: gameID})
	return md
}

// gameActiveFor builds a game_active=1 datapoint for gameID (session start +
// game_id adoption path).
func gameActiveFor(gameID string, active float64) pmetric.Metrics {
	md := pmetric.NewMetrics()
	addGauge(md, metricGameActive, active, map[string]string{attrGameID: gameID})
	return md
}

func sortedCopy(in []string) []string {
	out := append([]string(nil), in...)
	sort.Strings(out)
	return out
}

// TestMux_CrossGameBleed: interleaving two games' metric batches yields two
// disjoint partitions — no shared players, independent sessions, no last-writer
// wins on the session id.
func TestMux_CrossGameBleed(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, time.Hour, time.Hour, nil, nil)

	// Game A: start + player P1. Game B: start + player P2. Interleaved.
	mux.ApplyMetrics(gameActiveFor("game-A", 1))
	mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1.5))
	mux.ApplyMetrics(gameActiveFor("game-B", 1))
	mux.ApplyMetrics(playerIntensityFor("P2", "game-B", 2.5))
	// A second player joins A AFTER B started — must not leak into B.
	mux.ApplyMetrics(playerIntensityFor("P3", "game-A", 3.5))

	a, okA := mux.Snapshot("game-A")
	b, okB := mux.Snapshot("game-B")
	if !okA || !okB {
		t.Fatalf("both partitions must exist: okA=%v okB=%v", okA, okB)
	}

	if a.SessionID != "game-A" || b.SessionID != "game-B" {
		t.Fatalf("session ids leaked: A=%q B=%q", a.SessionID, b.SessionID)
	}
	if _, ok := a.Players["P1"]; !ok {
		t.Error("P1 should be in game-A")
	}
	if _, ok := a.Players["P3"]; !ok {
		t.Error("P3 should be in game-A")
	}
	if _, ok := a.Players["P2"]; ok {
		t.Error("P2 bled into game-A")
	}
	if _, ok := b.Players["P2"]; !ok {
		t.Error("P2 should be in game-B")
	}
	if len(b.Players) != 1 {
		t.Errorf("game-B should have exactly 1 player, got %d", len(b.Players))
	}

	parts := sortedCopy(mux.Partitions())
	if len(parts) != 2 || parts[0] != "game-A" || parts[1] != "game-B" {
		t.Fatalf("Partitions() = %v, want [game-A game-B]", parts)
	}
}

// TestMux_TouchedIDsDeduped: a single batch with datapoints for two games returns
// both ids exactly once.
func TestMux_TouchedIDsDeduped(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, time.Hour, time.Hour, nil, nil)

	md := pmetric.NewMetrics()
	addGauge(md, metricAccelMagnitude, 1, map[string]string{attrSerial: "P1", attrGameID: "game-A"})
	addGauge(md, metricAccelMagnitude, 2, map[string]string{attrSerial: "P2", attrGameID: "game-A"}) // same game again
	addGauge(md, metricAccelMagnitude, 3, map[string]string{attrSerial: "P3", attrGameID: "game-B"})

	got := sortedCopy(mux.ApplyMetrics(md))
	if len(got) != 2 || got[0] != "game-A" || got[1] != "game-B" {
		t.Fatalf("touched ids = %v, want [game-A game-B] (deduped)", got)
	}
}

// TestMux_FallbackPartitionZeroRegression: unlabeled batches route to the
// fallback partition and reproduce single-Store behavior; the touched id is the
// empty string.
func TestMux_FallbackPartitionZeroRegression(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, time.Hour, time.Hour, nil, nil)

	// Same sequence a single Store would receive, with NO game_id labels.
	single := NewStore(time.Hour, time.Hour, clk.now)

	mdActive := pmetric.NewMetrics()
	addGauge(mdActive, metricGameActive, 1, nil)
	mux.ApplyMetrics(mdActive)
	single.ApplyMetrics(cloneMetrics(mdActive))

	mdPlayer := pmetric.NewMetrics()
	addGauge(mdPlayer, metricAccelMagnitude, 4.2, serial("A"))
	mux.ApplyMetrics(mdPlayer)
	single.ApplyMetrics(cloneMetrics(mdPlayer))

	ids := mux.ApplyMetrics(playerIntensityFor("A", "", 4.2))
	if len(ids) != 1 || ids[0] != FallbackGameID {
		t.Fatalf("unlabeled touched ids = %v, want [%q]", ids, FallbackGameID)
	}

	fb, ok := mux.Snapshot(FallbackGameID)
	if !ok {
		t.Fatal("fallback partition must exist")
	}
	sg := single.Snapshot()
	// Byte-for-byte: same session-id synthesis, same player set, same intensity.
	if fb.SessionID != sg.SessionID {
		t.Errorf("fallback SessionID = %q, single = %q", fb.SessionID, sg.SessionID)
	}
	if len(fb.Players) != len(sg.Players) {
		t.Errorf("fallback players = %d, single = %d", len(fb.Players), len(sg.Players))
	}
	if got, want := *fb.Players["A"].MovementIntensity, *sg.Players["A"].MovementIntensity; got != want {
		t.Errorf("fallback intensity = %v, single = %v", got, want)
	}
	// Exactly one partition: the fallback.
	if parts := mux.Partitions(); len(parts) != 1 || parts[0] != FallbackGameID {
		t.Fatalf("Partitions() = %v, want [%q]", parts, FallbackGameID)
	}
}

// cloneMetrics deep-copies a pmetric.Metrics so the same payload can be fed to
// two independent stores without aliasing.
func cloneMetrics(md pmetric.Metrics) pmetric.Metrics {
	out := pmetric.NewMetrics()
	md.CopyTo(out)
	return out
}

// TestMux_EvictionRemovesEndedPartition: ending game A and advancing past grace
// removes A's partition entirely; B is untouched; the fallback survives even
// after its own session ends; a resumed signal for A recreates the partition.
func TestMux_EvictionRemovesEndedPartition(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, 5*time.Second, 15*time.Second, nil, nil)

	// A and B running with one player each. Fallback partition started + ended too.
	mux.ApplyMetrics(gameActiveFor("game-A", 1))
	mux.ApplyMetrics(playerIntensityFor("P1", "game-A", 1))
	mux.ApplyMetrics(gameActiveFor("game-B", 1))
	mux.ApplyMetrics(playerIntensityFor("P2", "game-B", 1))

	mdFbActive := pmetric.NewMetrics()
	addGauge(mdFbActive, metricGameActive, 1, nil)
	mux.ApplyMetrics(mdFbActive)
	mdFbEnd := pmetric.NewMetrics()
	addGauge(mdFbEnd, metricGameActive, 0, nil)
	mux.ApplyMetrics(mdFbEnd)

	// End game A.
	mux.ApplyMetrics(gameActiveFor("game-A", 0))

	// Advance past grace AND player TTL so A drains, but keep B fresh by re-pinging.
	clk.advance(20 * time.Second)
	mux.ApplyMetrics(playerIntensityFor("P2", "game-B", 1)) // refresh B's player

	mux.EvictStale()

	if _, ok := mux.Snapshot("game-A"); ok {
		t.Error("ended+drained partition game-A should be removed")
	}
	if _, ok := mux.Snapshot("game-B"); !ok {
		t.Error("fresh partition game-B must survive")
	}
	if _, ok := mux.Snapshot(FallbackGameID); !ok {
		t.Error("fallback partition must survive eviction even after its session ended")
	}

	// Signals for A resume -> partition is lazily recreated.
	mux.ApplyMetrics(gameActiveFor("game-A", 1))
	if _, ok := mux.Snapshot("game-A"); !ok {
		t.Error("resumed signals must lazily recreate game-A's partition")
	}
}

// TestMux_PerPartitionOnGameEnd: two games ending fire OnGameEnd once each, with
// the correct (distinct) session ids on the right partition's state.
func TestMux_PerPartitionOnGameEnd(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	var ends []string
	var endsMu sync.Mutex
	mux := newTestMux(clk, time.Hour, time.Hour, &ends, &endsMu)

	mux.ApplyMetrics(gameActiveFor("game-A", 1))
	mux.ApplyMetrics(gameActiveFor("game-B", 1))
	mux.ApplyMetrics(gameActiveFor("game-A", 0)) // A ends
	mux.ApplyMetrics(gameActiveFor("game-B", 0)) // B ends

	endsMu.Lock()
	got := sortedCopy(ends)
	endsMu.Unlock()
	if len(got) != 2 || got[0] != "game-A" || got[1] != "game-B" {
		t.Fatalf("OnGameEnd session ids = %v, want [game-A game-B]", got)
	}
}

// TestMux_DrainedFallbackNotRemoved: the fallback partition is never removed even
// when fully drained (ended past grace, no players).
func TestMux_DrainedFallbackNotRemoved(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, 5*time.Second, 10*time.Second, nil, nil)

	mdActive := pmetric.NewMetrics()
	addGauge(mdActive, metricGameActive, 1, nil)
	mux.ApplyMetrics(mdActive)
	mdEnd := pmetric.NewMetrics()
	addGauge(mdEnd, metricGameActive, 0, nil)
	mux.ApplyMetrics(mdEnd)

	clk.advance(20 * time.Second)
	mux.EvictStale()

	if _, ok := mux.Snapshot(FallbackGameID); !ok {
		t.Fatal("fallback partition must never be removed wholesale")
	}
}

// TestMux_ConcurrencySmoke: concurrent ApplyMetrics/ApplySpans across multiple
// game_ids plus EvictStale must be race-free (run with -race).
func TestMux_ConcurrencySmoke(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, time.Hour, time.Hour, nil, nil)

	games := []string{"game-A", "game-B", "game-C", ""}
	var wg sync.WaitGroup
	for w := 0; w < 8; w++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				g := games[(n+j)%len(games)]
				mux.ApplyMetrics(playerIntensityFor("P", g, float64(j)))
				mux.ApplyMetrics(gameActiveFor(g, float64(j%2)))
				td := spanForGame(g, "P")
				mux.ApplySpans(td)
				_, _ = mux.Snapshot(g)
				_ = mux.Partitions()
				if j%10 == 0 {
					mux.EvictStale()
				}
			}
		}(w)
	}
	wg.Wait()
}

// spanForGame builds a one-span trace carrying a serial + game.id (span routing).
func spanForGame(gameID, serial string) ptrace.Traces {
	td := ptrace.NewTraces()
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName("player_lifecycle")
	span.Attributes().PutStr(spanAttrSerial, serial)
	if gameID != "" {
		span.Attributes().PutStr(spanAttrGameID, gameID)
	}
	return td
}

// TestMux_SpanRouting: spans route to the partition named by their game.id, and
// label-free spans land in the fallback.
func TestMux_SpanRouting(t *testing.T) {
	clk := &muxClock{t: time.Unix(1000, 0)}
	mux := newTestMux(clk, time.Hour, time.Hour, nil, nil)

	if ids := mux.ApplySpans(spanForGame("game-A", "P1")); len(ids) != 1 || ids[0] != "game-A" {
		t.Fatalf("span touched ids = %v, want [game-A]", ids)
	}
	if ids := mux.ApplySpans(spanForGame("", "P2")); len(ids) != 1 || ids[0] != FallbackGameID {
		t.Fatalf("label-free span touched ids = %v, want [%q]", ids, FallbackGameID)
	}

	a, _ := mux.Snapshot("game-A")
	if _, ok := a.Players["P1"]; !ok {
		t.Error("P1 should be enriched into game-A from its span")
	}
	fb, _ := mux.Snapshot(FallbackGameID)
	if _, ok := fb.Players["P2"]; !ok {
		t.Error("P2 should be enriched into the fallback partition")
	}
	if _, ok := a.Players["P2"]; ok {
		t.Error("P2 bled into game-A")
	}
}
