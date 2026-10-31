package gamewindow

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/joustmania/agent/gamesummary"
)

// UC: UC-042 A12 BR-004
// The window keeps a game's prediction tally and nothing of the predictions
// themselves: a target-scope prediction names a serial.
func TestWindow_PredictionsKeepOnlyTally(t *testing.T) {
	t.Run("UC-042/A12 BR-004 the target serial is dropped, the tally kept", func(t *testing.T) {
		s := realGame()
		s.Predictions = []gamesummary.PredictionOutcome{
			{ID: "p1", Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "SERIAL-9F3A", P: 0.7, Acted: true, Outcome: "hit"},
			{ID: "p2", Backend: "rules", Source: "rules", Template: "survives", Scope: "target", Target: "SERIAL-1B2C", P: 0.7, Acted: true, Outcome: "miss"},
		}
		s.PredictionTally = gamesummary.TallyPredictions(s.Predictions)
		store := NewStore()
		store.Record(s)
		got := store.SnapshotFor("real", "")
		if len(got.Games) != 1 {
			t.Fatalf("window holds %d games, want 1", len(got.Games))
		}
		agg := got.Games[0]
		if len(agg.PredictionTally) != 1 || agg.PredictionTally[0].Hits != 1 || agg.PredictionTally[0].Misses != 1 {
			t.Fatalf("tally = %+v, want one group with 1 hit and 1 miss", agg.PredictionTally)
		}
		raw, err := json.Marshal(agg)
		if err != nil {
			t.Fatal(err)
		}
		for _, serial := range []string{"SERIAL-9F3A", "SERIAL-1B2C"} {
			if strings.Contains(string(raw), serial) {
				t.Errorf("UC-042/A12 BR-004: window aggregate holds %s: %s", serial, raw)
			}
		}
	})
	t.Run("UC-042/A12 a game without predictions has no tally", func(t *testing.T) {
		if agg := FromSummary(realGame()); agg.PredictionTally != nil {
			t.Errorf("tally = %+v, want nil", agg.PredictionTally)
		}
	})
}
