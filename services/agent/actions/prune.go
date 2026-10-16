package actions

import (
	"fmt"
	"sort"
	"strings"
)

// Pruning per-player entries (UC-040 A23 / BR-016, #1323).
//
// Every per-player write leaves one variant and one targeting rung for its serial
// in the per-player flags. Shadow serials are never reused, so without pruning
// the flags grow with every shadow game and flagd's evaluation cost grows with
// them. The Writer records which game each serial was last written for (owners)
// and the agent prunes a game's serials when the game ends or its partition is
// retired, plus once after startup, so the file holds entries for live games only.
//
// A prune is one read-modify-write of the whole file, in place, under the same
// mutex as decision writes, and is skipped when it would change nothing, so it
// never triggers a flagd reload on its own. Removing a serial resolves it to the
// flag's neutral variant on the coordinator, which never applies a neutral grant
// (BR-012) and resets levels to neutral (BR-017).

// gameIDPrefix is the coordinator's game id format (servicer.py: "game_<hex>").
// The agent's partition for unlabeled signals uses "session-<n>", which names no
// coordinator game.
const gameIDPrefix = "game_"

// targetedFlags lists every per-player flag, sorted for deterministic pruning.
func targetedFlags() []string {
	keys := make([]string, 0, len(targetedNeutral))
	for k := range targetedNeutral {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// validateGameID rejects an end_game address that is not a coordinator game id
// (UC-040 A18, #1323): an empty id would end the real game whichever loop
// decided it, and the fallback partition's "session-<n>" ends nothing while the
// write would still count as ok.
func validateGameID(gameID string) error {
	if gameID == "" {
		return fmt.Errorf("end_game requires the deciding game's id")
	}
	if !strings.HasPrefix(gameID, gameIDPrefix) || len(gameID) == len(gameIDPrefix) {
		return fmt.Errorf("end_game game id %q is not a coordinator game id (%s…); refusing an end that addresses no game", gameID, gameIDPrefix)
	}
	return nil
}

// recordOwner remembers the game a per-player write was for. Called with w.mu
// held (inside edit). An unaddressed write forgets the owner, so the serial is
// pruned by liveness only.
//
// The owner is the deciding loop's GameContext.SessionID. Until the partition
// adopts the coordinator's game_id label that is the synthetic "session-<n>", so
// a write made in that window is owned by "session-<n>". PruneGame for the
// game_ id neither matches it by owner nor treats it as unowned, so the game-end
// prune leaves it; the liveness prune (PruneExcept on partition retirement)
// removes it once its player is in no partition and "session-<n>" is no longer
// a live partition's SessionID.
func (w *Writer) recordOwner(serial, gameID string) {
	if w.owners == nil {
		w.owners = map[string]string{}
	}
	if gameID == "" {
		delete(w.owners, serial)
		return
	}
	w.owners[serial] = gameID
}

// dropSerialFromFlag removes every variant of serial from one per-player flag and
// rebuilds its ladder. It reports whether the flag changed. Shared by
// RevertTargeted and the prunes.
func dropSerialFromFlag(doc *orderedDoc, flagKey string, serials map[string]bool) (bool, error) {
	neutral, ok := targetedNeutral[flagKey]
	if !ok {
		return false, fmt.Errorf("%q is not a per-player state flag", flagKey)
	}
	f, err := doc.flag(flagKey)
	if err != nil {
		return false, err
	}
	variants := map[string]any{}
	if _, err := f.get("variants", &variants); err != nil {
		return false, err
	}
	before := len(variants)
	for name := range variants {
		if s, ok := agentSerial(name); ok && serials[s] {
			delete(variants, name)
		}
	}
	if len(variants) == before {
		return false, nil
	}
	if err := f.set("variants", variants); err != nil {
		return false, err
	}
	if err := setLadder(f, variantNames(variants), neutral); err != nil {
		return false, err
	}
	return true, doc.putFlag(flagKey, f)
}

// targetedSerials lists every serial that has an entry in any per-player flag.
func targetedSerials(doc *orderedDoc) (map[string]bool, error) {
	out := map[string]bool{}
	for _, key := range targetedFlags() {
		f, err := doc.flag(key)
		if err != nil {
			return nil, err
		}
		variants := map[string]any{}
		if _, err := f.get("variants", &variants); err != nil {
			return nil, err
		}
		for name := range variants {
			if s, ok := agentSerial(name); ok {
				out[s] = true
			}
		}
	}
	return out, nil
}

// pruneSerials removes the given serials from every per-player flag and forgets
// their owners. Called with w.mu held. Reports whether the document changed.
func (w *Writer) pruneSerials(doc *orderedDoc, serials map[string]bool) (bool, error) {
	changed := false
	for _, key := range targetedFlags() {
		c, err := dropSerialFromFlag(doc, key, serials)
		if err != nil {
			return false, err
		}
		changed = changed || c
	}
	for s := range serials {
		delete(w.owners, s)
	}
	return changed, nil
}

// PruneGame removes the per-player entries of a game that ended (UC-040 A23 step
// 1): every serial last written for gameID, plus every player of the game that
// the Writer has no owner for (for example after an agent restart) unless it is
// in protect, the players of other active games. A serial last written for
// another game is never removed. It returns the number of serials removed from
// the file; nothing is written when that is zero. Safe for concurrent use.
func (w *Writer) PruneGame(gameID string, players []string, protect map[string]bool) (int, error) {
	removed := 0
	err := w.editIfChanged(func(doc *orderedDoc) (bool, error) {
		present, err := targetedSerials(doc)
		if err != nil {
			return false, err
		}
		drop := map[string]bool{}
		for s, g := range w.owners {
			if gameID != "" && g == gameID {
				drop[s] = true
			}
		}
		for _, s := range players {
			if _, owned := w.owners[s]; !owned && !protect[s] {
				drop[s] = true
			}
		}
		for s := range drop {
			if present[s] {
				removed++
			}
		}
		return w.pruneSerials(doc, drop)
	})
	if err != nil {
		return 0, err
	}
	return removed, nil
}

// PruneExcept removes every per-player entry whose serial is not in keep and
// whose owner game is not in liveGames (UC-040 A23 step 2: partition retired, or
// once after startup). It returns the number of serials removed; nothing is
// written when that is zero. Safe for concurrent use.
func (w *Writer) PruneExcept(keep, liveGames map[string]bool) (int, error) {
	removed := 0
	err := w.editIfChanged(func(doc *orderedDoc) (bool, error) {
		present, err := targetedSerials(doc)
		if err != nil {
			return false, err
		}
		drop := map[string]bool{}
		for s := range present {
			if keep[s] {
				continue
			}
			if g, owned := w.owners[s]; owned && liveGames[g] {
				continue
			}
			drop[s] = true
		}
		removed = len(drop)
		return w.pruneSerials(doc, drop)
	})
	if err != nil {
		return 0, err
	}
	return removed, nil
}
