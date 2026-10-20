package llm

import "testing"

// The coordinator emits game.mode (span) and game_current_mode{mode} (metric) as
// the built game's get_game_name() ("FFA", "Zombie", ...). normalizeMode must map
// that vocabulary to the mode prompt rules. Menu enum names such as "JoustFFA"
// are not recognised, so the coordinator must never put them on the mode label.
// UC: UC-040 main
func TestNormalizeMode_CoordinatorGameNames(t *testing.T) {
	for raw, want := range map[string]string{
		"FFA":        "ffa",
		"Zombie":     "zombie",
		"Swapper":    "swapper",
		"Tournament": "tournament",
		"JoustFFA":   "",
	} {
		t.Run("UC-040/main "+raw, func(t *testing.T) {
			if got := normalizeMode(raw); got != want {
				t.Fatalf("normalizeMode(%q) = %q, want %q", raw, got, want)
			}
		})
	}
}
