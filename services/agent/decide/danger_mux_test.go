package decide

import (
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/pmetric"

	"github.com/joustmania/agent/gamecontext"
)

func addMuxGauge(md pmetric.Metrics, name string, value float64, attrs map[string]string) {
	m := md.ResourceMetrics().AppendEmpty().ScopeMetrics().AppendEmpty().Metrics().AppendEmpty()
	m.SetName(name)
	dp := m.SetEmptyGauge().DataPoints().AppendEmpty()
	dp.SetDoubleValue(value)
	for k, v := range attrs {
		dp.Attributes().PutStr(k, v)
	}
}

// playersFor exports two live players of one game: A calm, B near the threshold.
func playersFor(md pmetric.Metrics, gameID string) {
	for serial, accel := range map[string]float64{"A": 0.5, "B": 1.9} {
		labels := map[string]string{"serial": serial, "game_id": gameID}
		addMuxGauge(md, "game_player_alive", 1, labels)
		addMuxGauge(md, "game_player_accel_magnitude", accel, labels)
	}
}

func dangerOf(t *testing.T, mux *gamecontext.Multiplexer, gameID string) map[string]string {
	t.Helper()
	snap, ok := mux.Snapshot(gameID)
	if !ok {
		t.Fatalf("no partition for %s", gameID)
	}
	out := map[string]string{}
	for _, p := range BuildState(snap).Players {
		out[p.Serial] = p.Danger
	}
	return out
}

// A game_id-labelled death threshold (FR-041) reaches the game's partition, so the
// danger classification works per game. Before #1336 the threshold was unlabelled,
// landed in the fallback partition, and every game's danger stayed unknown.
// UC-040 main step 4: the built state carries each player's danger class.
// UC: UC-040 main
func TestDanger_LabelledThresholdReachesGamePartition(t *testing.T) {
	newMux := func() *gamecontext.Multiplexer {
		return gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
			return gamecontext.NewStore(time.Hour, time.Hour, time.Now)
		})
	}
	t.Run("UC-040/main labelled threshold classifies danger in the game partition", func(t *testing.T) {
		mux := newMux()
		md := pmetric.NewMetrics()
		playersFor(md, "game-real")
		addMuxGauge(md, "game_effective_death_threshold", 2.0,
			map[string]string{"game_id": "game-real", "game_kind": "primary"})
		mux.ApplyMetrics(md)
		got := dangerOf(t, mux, "game-real")
		if got["A"] != DangerSafe || got["B"] != DangerNearThreshold {
			t.Fatalf("danger = %v, want A=%s B=%s", got, DangerSafe, DangerNearThreshold)
		}
	})
	t.Run("UC-040/main unlabelled threshold leaves the game partition unclassified", func(t *testing.T) {
		mux := newMux()
		md := pmetric.NewMetrics()
		playersFor(md, "game-real")
		addMuxGauge(md, "game_effective_death_threshold", 2.0, nil)
		mux.ApplyMetrics(md)
		if got := dangerOf(t, mux, "game-real"); got["A"] != "" || got["B"] != "" {
			t.Fatalf("danger = %v, want unknown (threshold went to the fallback partition)", got)
		}
	})
}
