package actions

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
)

// perPlayerDecisions is one decision per per-player flag (two levels, three grants).
var perPlayerDecisions = []string{
	decision.InterventionAdjustPlayerSensitivity,
	decision.InterventionSetPlayerHandicap,
	decision.InterventionGrantShield,
	decision.InterventionPartialShield,
	decision.InterventionSoftPenalty,
}

// writeAllPerPlayer applies every per-player intervention for serial in gameID.
func writeAllPerPlayer(t *testing.T, w *Writer, gameID, serial string) {
	t.Helper()
	for _, iv := range perPlayerDecisions {
		if err := w.Apply(context.Background(), decision.Decision{Intervention: iv, TargetSerial: serial, GameID: gameID}); err != nil {
			t.Fatalf("apply %s for %s: %v", iv, serial, err)
		}
	}
}

// serialsIn returns the serials that have a variant in flagKey.
func serialsIn(t *testing.T, path, flagKey string) map[string]bool {
	t.Helper()
	out := map[string]bool{}
	variants, _ := readFlag(t, path, flagKey)["variants"].(map[string]any)
	for name := range variants {
		if s, ok := agentSerial(name); ok {
			out[s] = true
		}
	}
	return out
}

// assertSerials checks that every per-player flag holds exactly want.
func assertSerials(t *testing.T, path string, want ...string) {
	t.Helper()
	for _, key := range targetedFlags() {
		got := serialsIn(t, path, key)
		if len(got) != len(want) {
			t.Fatalf("%s holds serials %v, want %v", key, got, want)
		}
		for _, s := range want {
			if !got[s] {
				t.Fatalf("%s holds serials %v, want %v", key, got, want)
			}
		}
		f := readFlag(t, path, key)
		_, hasLadder := f["targeting"]
		if hasLadder != (len(want) > 0) {
			t.Fatalf("%s targeting present=%v with serials %v", key, hasLadder, want)
		}
	}
}

func readBytes(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// UC: UC-040 A23 BR-016
func TestPruneGameRemovesTheEndedGamesEntries(t *testing.T) {
	t.Run("UC-040/A23 game end prunes the game's serials from all five per-player flags", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_a", "A1")
		writeAllPerPlayer(t, w, "game_a", "A2")
		writeAllPerPlayer(t, w, "game_b", "B1")

		n, err := w.PruneGame("game_a", []string{"A1", "A2"}, nil)
		if err != nil {
			t.Fatal(err)
		}
		if n != 2 {
			t.Fatalf("removed %d serials, want 2", n)
		}
		assertSerials(t, path, "B1")
		assertTargetingResolves(t, path, flagShieldSeconds, "B1", grantName(t, path, flagShieldSeconds, "B1"))
		// A pruned player falls through to the neutral variant (never re-fired, BR-012).
		assertTargetingResolves(t, path, flagShieldSeconds, "A1", neutralNone)
		assertTargetingResolves(t, path, flagPlayerSensitivityFactor, "A1", neutralDefault)
		assertStructurallyValid(t, path)
	})

	t.Run("UC-040/A23 the last game's prune drops the targeting block entirely", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_a", "A1")
		if _, err := w.PruneGame("game_a", nil, nil); err != nil {
			t.Fatal(err)
		}
		assertSerials(t, path)
		for _, key := range targetedFlags() {
			if got := readFlag(t, path, key)["defaultVariant"]; got != targetedNeutral[key] {
				t.Fatalf("%s defaultVariant = %v, want neutral %q", key, got, targetedNeutral[key])
			}
		}
	})

	t.Run("UC-040/A23 a serial last written for another game is kept even if it played the ended game", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_a", "P1")
		writeAllPerPlayer(t, w, "game_b", "P1") // the controller moved on to game_b
		if _, err := w.PruneGame("game_a", []string{"P1"}, nil); err != nil {
			t.Fatal(err)
		}
		assertSerials(t, path, "P1")
	})

	t.Run("UC-040/A23 without an owner record (agent restart) players of other active games are protected", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_a", "P1")
		writeAllPerPlayer(t, w, "game_a", "P2")
		restarted := NewWriter(path, discardLogger())
		n, err := restarted.PruneGame("game_a", []string{"P1", "P2"}, map[string]bool{"P2": true})
		if err != nil {
			t.Fatal(err)
		}
		if n != 1 {
			t.Fatalf("removed %d serials, want 1", n)
		}
		assertSerials(t, path, "P2")
	})
}

// UC: UC-040 A23 BR-016
func TestPruneWritesNothingWhenNothingChanges(t *testing.T) {
	t.Run("UC-040/BR-016 a prune with nothing to remove leaves the file and its mtime untouched", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_b", "B1")
		old := time.Now().Add(-time.Hour)
		if err := os.Chtimes(path, old, old); err != nil {
			t.Fatal(err)
		}
		before := readBytes(t, path)
		for name, prune := range map[string]func() (int, error){
			"PruneGame":   func() (int, error) { return w.PruneGame("game_a", []string{"A1"}, nil) },
			"PruneExcept": func() (int, error) { return w.PruneExcept(map[string]bool{"B1": true}, nil) },
			"RevertTargeted": func() (int, error) {
				return 0, w.RevertTargeted(flagShieldSeconds, "A1")
			},
		} {
			n, err := prune()
			if err != nil || n != 0 {
				t.Fatalf("%s = (%d, %v), want (0, nil)", name, n, err)
			}
		}
		if !bytes.Equal(before, readBytes(t, path)) {
			t.Fatal("no-op prune rewrote the file")
		}
		info, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if !info.ModTime().Equal(old) {
			t.Fatal("no-op prune bumped the mtime; flagd would reload for nothing")
		}
	})
}

// UC: UC-040 A23 BR-016
func TestPerPlayerFlagsStayBoundedAcrossGames(t *testing.T) {
	t.Run("UC-040/BR-016 shadow games with fresh serials: the file holds only the live game after N games", func(t *testing.T) {
		w, path := newTestWriter(t)
		const games = 40
		var sizeAfterFirst int
		for g := 0; g < games; g++ {
			gameID := fmt.Sprintf("game_shadow%03d", g)
			players := []string{fmt.Sprintf("MOCK%04d", 2*g), fmt.Sprintf("MOCK%04d", 2*g+1)}
			for _, s := range players {
				writeAllPerPlayer(t, w, gameID, s)
			}
			if _, err := w.PruneGame(gameID, players, nil); err != nil {
				t.Fatal(err)
			}
			if g == 0 {
				sizeAfterFirst = len(readBytes(t, path))
			}
		}
		assertSerials(t, path)
		if size := len(readBytes(t, path)); size != sizeAfterFirst {
			t.Fatalf("file size after %d games = %d bytes, after 1 game = %d: it grows with history", games, size, sizeAfterFirst)
		}
	})

	t.Run("UC-040/BR-016 real games reuse serials: each game end clears the previous game's entries", func(t *testing.T) {
		w, path := newTestWriter(t)
		players := []string{"00:06:f7:aa:00:01", "00:06:f7:aa:00:02"}
		for g := 0; g < 5; g++ {
			gameID := fmt.Sprintf("game_real%03d", g)
			writeAllPerPlayer(t, w, gameID, players[g%2])
			if _, err := w.PruneGame(gameID, players, nil); err != nil {
				t.Fatal(err)
			}
			assertSerials(t, path)
		}
	})
}

// UC: UC-040 A23 BR-016
func TestPruneExceptKeepsLiveGames(t *testing.T) {
	t.Run("UC-040/A23 startup prune drops every entry of no live game", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_old", "OLD1")
		writeAllPerPlayer(t, w, "game_live", "LIVE1")
		restarted := NewWriter(path, discardLogger()) // owners lost
		n, err := restarted.PruneExcept(map[string]bool{"LIVE1": true}, map[string]bool{"game_live": true})
		if err != nil {
			t.Fatal(err)
		}
		if n != 1 {
			t.Fatalf("removed %d serials, want 1", n)
		}
		assertSerials(t, path, "LIVE1")
	})

	t.Run("UC-040/A23 an entry whose game is still a live partition survives its player going silent", func(t *testing.T) {
		w, path := newTestWriter(t)
		writeAllPerPlayer(t, w, "game_live", "SILENT")
		if _, err := w.PruneExcept(nil, map[string]bool{"game_live": true}); err != nil {
			t.Fatal(err)
		}
		assertSerials(t, path, "SILENT")
		if _, err := w.PruneExcept(nil, nil); err != nil {
			t.Fatal(err)
		}
		assertSerials(t, path)
	})
}

// UC: UC-040 A18 BR-011
func TestEndGameRefusesNonGameIDs(t *testing.T) {
	for _, id := range []string{"session-3", "", "game_", "GAME_abc", "shadow-1"} {
		t.Run(fmt.Sprintf("UC-040/A18 end_game for %q is refused, recorded as error and writes nothing", id), func(t *testing.T) {
			w, path, reader := newMeteredWriter(t)
			before := readBytes(t, path)
			if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame, GameID: id}); err == nil {
				t.Fatalf("end_game for %q was written; it addresses no coordinator game", id)
			}
			if !bytes.Equal(before, readBytes(t, path)) {
				t.Fatal("refused end_game modified the interventions file")
			}
			if got := writesByResult(t, reader); got[resultError] != 1 || got[resultOK] != 0 {
				t.Fatalf("writes = %v, want one error and no ok", got)
			}
		})
	}
	t.Run("UC-040/A18 end_game for a coordinator game id is written", func(t *testing.T) {
		w, path := newTestWriter(t)
		if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionEndGame, GameID: "game_0123abcd"}); err != nil {
			t.Fatal(err)
		}
		if _, game := splitNonce(activeValueOf(t, path, flagEndGame)); game != "game_0123abcd" {
			t.Fatalf("end_game payload = %q", game)
		}
	})
}

// UC: UC-040 A19 BR-012
func TestDoubleGrantWithinOnePollKeepsTheLaterGrant(t *testing.T) {
	t.Run("UC-040/BR-012 two grants to one player before flagd reloads leave only the later one", func(t *testing.T) {
		w, path := newTestWriter(t)
		for _, v := range []string{"10", "3"} {
			if err := w.Apply(context.Background(), decision.Decision{Intervention: decision.InterventionGrantShield, TargetSerial: "P1", Value: v, GameID: "game_a"}); err != nil {
				t.Fatal(err)
			}
		}
		if got := grantValue(t, path, flagShieldSeconds, "P1"); got != float64(3) {
			t.Fatalf("shield for P1 = %v, want the later grant 3", got)
		}
		if n := len(serialsIn(t, path, flagShieldSeconds)); n != 1 {
			t.Fatalf("%d entries for one player", n)
		}
	})
}
