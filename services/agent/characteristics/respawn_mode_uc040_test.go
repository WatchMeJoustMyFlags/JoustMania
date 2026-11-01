package characteristics

import (
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 BR-043
// TestRespawnMode: the coordinator's mode names and the menu aliases of NonStop,
// Zombies and Swapper are respawn modes; every other mode and an unobserved one
// are not, and a respawn mode has no elimination_pace (#2532).
func TestRespawnMode(t *testing.T) {
	cases := map[string]bool{
		"Nonstop Joust": true, "NonStop": true, "Zombie": true, "zombies": true, "Swapper": true,
		"FFA": false, "Teams": false, "Fight Club": false, "Tournament": false, "": false,
	}
	for mode, want := range cases {
		t.Run("UC-040/BR-043 mode "+mode, func(t *testing.T) {
			m := mode
			c := gamecontext.GameContext{}
			c.Session.GameMode = &m
			if got := RespawnMode(c); got != want {
				t.Fatalf("RespawnMode(%q) = %v, want %v", mode, got, want)
			}
			w := Window{From: time.Unix(0, 0), To: time.Unix(60, 0)}
			if _, ok := EliminationPace(c, w); ok == want {
				t.Fatalf("EliminationPace ok = %v in %q", ok, mode)
			}
		})
	}
	t.Run("UC-040/BR-043 an unobserved mode is not a respawn mode", func(t *testing.T) {
		if RespawnMode(gamecontext.GameContext{}) {
			t.Fatal("nil mode must not be a respawn mode")
		}
	})
}
