package main

import (
	"slices"

	"github.com/joustmania/agent/gamerunner"
)

// catalogKeyModes is the one table of experiment-catalog keys that change a game
// only in some game modes (#2392), keyed by flag with the Games enum names
// (lib/types.py) of the modes that read and act on it. A catalog key without an
// entry acts in every mode. The dynamic declarer and the proposer offer a key only
// when the shadow game's mode is listed (offeredInShadowMode; UC-043 BR-009,
// UC-042 step 4): shadow games are JoustFFA only today (UC-046 BR-003), so none of
// these keys is offered, and a shadow game in another mode later gets its keys
// without a change here. TestUC043_BR009_CatalogKeysReachTheShadowGame checks the
// table against the coordinator's reads in both directions.
var catalogKeyModes = map[string][]string{
	// Read in games/base.py for every mode, but only the Swapper team swap and the
	// Zombies turn and respawn use it (self.death_grace_period); FFA never respawns.
	"death_grace_period_seconds":              {"Swapper", "Zombies"},
	"nonstop.respawn_seconds":                 {"NonStop"},
	"nonstop.spawn_protection_seconds":        {"NonStop"},
	"fight_club.round_seconds":                {"FightClub"},
	"tournament.match_seconds":                {"Tournament"},
	"tournament.time_between_matches_seconds": {"Tournament"},
	"werewolf.werewolf_fraction":              {"Werewolf"},
	"zombie.initial_count":                    {"Zombies"},
	"zombie.respawn_min_seconds":              {"Zombies"},
	"zombie.respawn_max_seconds":              {"Zombies"},
}

// offeredInShadowMode reports whether an experiment on key can change a shadow game
// of the given mode: the key has no mode restriction, or the mode is one of its
// modes (catalogKeyModes).
func offeredInShadowMode(key, mode string) bool {
	modes, restricted := catalogKeyModes[key]
	return !restricted || slices.Contains(modes, mode)
}

// shadowGameMode is the mode every experiment shadow game starts in: the shadow
// spawner's template spec (SHADOW_GAME_MODE, default JoustFFA). The declarer and
// the proposer read it on each call, so they filter for the mode the spawner uses.
func shadowGameMode() string {
	return gamerunner.SpecFromEnv("").GameName
}
