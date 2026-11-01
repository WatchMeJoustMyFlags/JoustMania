package characteristics

import (
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 BR-043
// TestRespawnMode: the coordinator's mode names and the menu aliases of NonStop,
// Zombies, Swapper and Fight Club are respawn modes; every other mode and an
// unobserved one are not (#2532).
func TestRespawnMode(t *testing.T) {
	cases := map[string]bool{
		"Nonstop Joust": true, "NonStop": true, "Zombie": true, "zombies": true, "Swapper": true,
		"Fight Club": true, "fight_club": true,
		"FFA": false, "Teams": false, "Random Teams": false, "Tournament": false, "Traitor": false,
		"Werewolf": false, "": false,
	}
	for mode, want := range cases {
		t.Run("UC-040/BR-043 mode "+mode, func(t *testing.T) {
			m := mode
			c := gamecontext.GameContext{}
			c.Session.GameMode = &m
			if got := RespawnMode(c); got != want {
				t.Fatalf("RespawnMode(%q) = %v, want %v", mode, got, want)
			}
		})
	}
	t.Run("UC-040/BR-043 an unobserved mode is not a respawn mode", func(t *testing.T) {
		if RespawnMode(gamecontext.GameContext{}) {
			t.Fatal("nil mode must not be a respawn mode")
		}
	})
}
