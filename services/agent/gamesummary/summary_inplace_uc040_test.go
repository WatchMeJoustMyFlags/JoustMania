package gamesummary

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// inPlaceTimeline is one dispatched music_tempo_override at +15s (intensity 0.41 ->
// 0.81, effect +0.40) and one "already in place" entry of the same type at +100s
// (UC-040 A35) with intensity 0.80 -> 0.20 around it.
func inPlaceTimeline() []gamecontext.TimelineEvent {
	return []gamecontext.TimelineEvent{
		{At: at(0), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.40)},
		{At: at(10), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.42)},
		{At: at(15), Kind: gamecontext.EventIntervention, Detail: "music_tempo_override"},
		{At: at(20), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.80)},
		{At: at(25), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.82)},
		{At: at(90), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.80)},
		{At: at(100), Kind: gamecontext.EventIntervention, Detail: "music_tempo_override", InPlace: true},
		{At: at(110), Kind: gamecontext.EventStateDelta, ActivePlayers: iptr(3), MeanIntensity: fptr(0.20)},
	}
}

// UC: UC-040 A35
func TestBuildSummary_InPlaceIsNotAnIntervention(t *testing.T) {
	t.Run("UC-040/A35 an already-in-place entry is not dispatched and has no effect", func(t *testing.T) {
		gc := gamecontext.GameContext{
			SessionID: "game_inplace",
			Session:   gamecontext.SessionSignals{GameActive: bptr(false)},
			Timeline:  inPlaceTimeline(),
		}
		got := BuildSummary(gc, BuildOptions{Now: at(120)})
		if len(got.Interventions) != 1 {
			t.Fatalf("Interventions = %+v, want only the dispatched one", got.Interventions)
		}
		iv := got.Interventions[0]
		if !iv.Dispatched || iv.OffsetSeconds != 15 {
			t.Errorf("intervention = %+v, want the dispatched one at +15s", iv)
		}
	})
}

