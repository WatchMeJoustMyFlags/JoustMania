package main

import (
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"

	"github.com/joustmania/agent/gamecontext"
)

func gameActiveMetric(gameID string) pmetric.Metrics {
	md := pmetric.NewMetrics()
	m := md.ResourceMetrics().AppendEmpty().ScopeMetrics().AppendEmpty().Metrics().AppendEmpty()
	m.SetName("game_active")
	dp := m.SetEmptyGauge().DataPoints().AppendEmpty()
	dp.SetDoubleValue(1)
	dp.Attributes().PutStr("game_id", gameID)
	return md
}

// UC: UC-046 A8 BR-008
func TestOtherGameLive_IgnoresStalePartitions(t *testing.T) {
	t.Run("UC-046/A8 a fresh other game is live; a stale partition is not", func(t *testing.T) {
		clock := time.Unix(10_000, 0)
		now := func() time.Time { return clock }
		mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
			return gamecontext.NewStore(time.Hour, time.Hour, now)
		})
		mux.ApplyMetrics(gameActiveMetric("game_pattern"))
		mux.ApplyMetrics(gameActiveMetric("game_other"))
		live := otherGameLiveAt(mux, now)
		if !live("game_pattern") {
			t.Fatal("a fresh other game was not seen as live")
		}
		clock = clock.Add(liveStaleness + time.Second)
		if live("game_pattern") {
			t.Fatal("a stale partition (no update for > 10 s) still counted as live")
		}
		mux.ApplyMetrics(gameActiveMetric("game_pattern")) // only this game is fresh again
		if live("game_pattern") {
			t.Fatal("the pattern game's own partition counted as another game")
		}
	})
}
