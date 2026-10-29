package main

import (
	"slices"
	"sort"
	"testing"

	"github.com/joustmania/agent/decision"
)

// ffaNeverOffered are the catalog keys the maintainer decision on #2392 names as
// never changing a JoustFFA game: nine keys only their own mode reads, and
// death_grace_period_seconds, which base.py reads but only the Swapper and Zombies
// respawn paths use.
var ffaNeverOffered = []string{
	"death_grace_period_seconds",
	"fight_club.round_seconds",
	"nonstop.respawn_seconds",
	"nonstop.spawn_protection_seconds",
	"tournament.match_seconds",
	"tournament.time_between_matches_seconds",
	"werewolf.werewolf_fraction",
	"zombie.initial_count",
	"zombie.respawn_max_seconds",
	"zombie.respawn_min_seconds",
}

// modeOnlyKeys are the keys catalogKeyModes restricts plus ffaNeverOffered, sorted.
func modeOnlyKeys() []string {
	keys := slices.Clone(ffaNeverOffered)
	for k := range catalogKeyModes {
		if !slices.Contains(keys, k) {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	return keys
}

// allDeclaredKeys runs the declarer until it has nothing left to declare and returns
// every key it offered (each declared key is marked active, so the next Declare
// moves on).
func allDeclaredKeys(t *testing.T, d *dynamicDeclarer) []string {
	t.Helper()
	active := activeFlags{}
	var keys []string
	for range 100 {
		intent, ok := d.Declare(active)
		if !ok {
			return keys
		}
		keys = append(keys, intent.FlagKey)
		active[intent.FlagKey] = struct{}{}
	}
	t.Fatal("declarer never ran out of keys")
	return nil
}

// The dynamic declarer and the proposer offer a catalog key only when the shadow
// game's mode reads it (#2392): a JoustFFA shadow game is never offered a key only
// another mode reads (the nine mode-only keys and death_grace_period_seconds),
// and a shadow game in that mode would be.
//
// UC: UC-043 BR-009
// UC: UC-042 main
func TestUC043_BR009_OnlyKeysTheShadowModeReadsAreOffered_2392(t *testing.T) {
	svc := gameServiceForDeclarer(t)
	newDeclarer := func(candidates []string) *dynamicDeclarer {
		return &dynamicDeclarer{gameFlags: svc, candidates: candidates, defaultObjective: decision.ObjectiveBalanced}
	}

	t.Run("UC-043/BR-009 the declarer never offers a mode-only key for JoustFFA", func(t *testing.T) {
		t.Setenv("SHADOW_GAME_MODE", "")
		offered := allDeclaredKeys(t, newDeclarer(nil))
		if len(offered) == 0 {
			t.Fatal("declarer offered nothing: catalog not read")
		}
		for _, k := range offered {
			if slices.Contains(modeOnlyKeys(), k) {
				t.Errorf("declarer offered %s to a JoustFFA shadow game", k)
			}
		}
		if intent, ok := newDeclarer(modeOnlyKeys()).Declare(nil); ok {
			t.Errorf("declarer offered %s from a mode-only candidate list", intent.FlagKey)
		}
	})
	t.Run("UC-042/main the proposer vocabulary has no mode-only key for JoustFFA", func(t *testing.T) {
		t.Setenv("SHADOW_GAME_MODE", "")
		vocab := gameFlagVocab(svc, nil)()
		if _, ok := vocab["sensitivity"]; !ok {
			t.Fatalf("vocab %v misses sensitivity: catalog not read", vocab)
		}
		for _, k := range modeOnlyKeys() {
			if _, ok := vocab[k]; ok {
				t.Errorf("proposer vocabulary offers %s to a JoustFFA shadow game", k)
			}
		}
	})
	t.Run("UC-043/BR-009 a shadow game in another mode gets that mode's keys", func(t *testing.T) {
		t.Setenv("SHADOW_GAME_MODE", "NonStop")
		want := []string{"nonstop.respawn_seconds", "nonstop.spawn_protection_seconds"}
		vocab := gameFlagVocab(svc, nil)()
		offered := allDeclaredKeys(t, newDeclarer(nil))
		for _, k := range want {
			if _, ok := vocab[k]; !ok {
				t.Errorf("proposer vocabulary misses %s for a NonStop shadow game", k)
			}
			if !slices.Contains(offered, k) {
				t.Errorf("declarer never offered %s for a NonStop shadow game: %v", k, offered)
			}
		}
		if _, ok := vocab["zombie.initial_count"]; ok {
			t.Error("proposer vocabulary offers a Zombies key to a NonStop shadow game")
		}
	})
}
