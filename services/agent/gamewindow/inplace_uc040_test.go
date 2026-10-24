package gamewindow

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
)

// UC: UC-040 A35
func TestFromSummary_InPlaceAddsNoEffect(t *testing.T) {
	t.Run("UC-040/A35 an already-in-place entry adds no effect sample to the type's mean", func(t *testing.T) {
		base := time.Date(2026, 6, 12, 10, 0, 0, 0, time.UTC)
		at := func(sec int) time.Time { return base.Add(time.Duration(sec) * time.Second) }
		delta := func(sec int, intensity float64) gamecontext.TimelineEvent {
			return gamecontext.TimelineEvent{At: at(sec), Kind: gamecontext.EventStateDelta,
				ActivePlayers: intp(3), MeanIntensity: f64(intensity)}
		}
		ended := false
		gc := gamecontext.GameContext{
			SessionID: "game_inplace",
			GameKind:  gamecontext.GameKindReal,
			Session:   gamecontext.SessionSignals{GameActive: &ended},
			// A dispatched tempo change measured +0.40, then the same value already in
			// place at +100s while intensity fell 0.80 -> 0.20.
			Timeline: []gamecontext.TimelineEvent{
				delta(0, 0.40), delta(10, 0.42),
				{At: at(15), Kind: gamecontext.EventIntervention, Detail: "music_tempo_override"},
				delta(20, 0.80), delta(25, 0.82), delta(90, 0.80),
				{At: at(100), Kind: gamecontext.EventIntervention, Detail: "music_tempo_override", InPlace: true},
				delta(110, 0.20),
			},
		}
		agg := FromSummary(gamesummary.BuildSummary(gc, gamesummary.BuildOptions{Now: at(120)}))
		var tempo *InterventionAggregate
		for i := range agg.Interventions {
			if agg.Interventions[i].Type == "music_tempo_override" {
				tempo = &agg.Interventions[i]
			}
		}
		if tempo == nil || tempo.MeanIntensityDelta == nil {
			t.Fatalf("music_tempo_override aggregate = %+v, want a mean intensity delta", tempo)
		}
		if d := *tempo.MeanIntensityDelta; d < 0.39 || d > 0.41 {
			t.Errorf("MeanIntensityDelta = %v, want ~0.40 (the dispatched entry only)", d)
		}
		if tempo.Blocked != 0 {
			t.Errorf("Blocked = %d, want 0: an entry already in place is not blocked", tempo.Blocked)
		}
	})
}
