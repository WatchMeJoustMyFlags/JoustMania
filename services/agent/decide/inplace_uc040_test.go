package decide

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 A35
func TestRecentlyShielded_InPlaceIsNoShield(t *testing.T) {
	t.Run("UC-040/A35 a toughness_boost already in place does not mark the player shielded", func(t *testing.T) {
		now := time.Date(2026, 6, 12, 10, 0, 0, 0, time.UTC)
		c := gamecontext.GameContext{UpdatedAt: now, Timeline: []gamecontext.TimelineEvent{
			{At: now.Add(-2 * time.Second), Kind: gamecontext.EventIntervention, Detail: "toughness_boost", Serial: "AA", InPlace: true},
			{At: now.Add(-2 * time.Second), Kind: gamecontext.EventIntervention, Detail: "toughness_boost", Serial: "BB"},
		}}
		got := recentlyShielded(c, nil)
		if got["AA"] || !got["BB"] {
			t.Errorf("shielded = %v, want only BB", got)
		}
	})
}
