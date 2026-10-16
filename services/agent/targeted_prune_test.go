package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/gamecontext"
)

// fakeParts is a static partitionSource.
type fakeParts struct {
	mu    sync.Mutex
	snaps map[string]gamecontext.GameContext
}

func (f *fakeParts) Partitions() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	ids := make([]string, 0, len(f.snaps))
	for id := range f.snaps {
		ids = append(ids, id)
	}
	return ids
}

func (f *fakeParts) Snapshot(id string) (gamecontext.GameContext, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	s, ok := f.snaps[id]
	return s, ok
}

func gameCtx(sessionID string, active bool, serials ...string) gamecontext.GameContext {
	c := gamecontext.GameContext{SessionID: sessionID, Players: map[string]*gamecontext.PlayerSignals{}}
	c.Session.GameActive = &active
	for _, s := range serials {
		c.Players[s] = &gamecontext.PlayerSignals{Serial: s}
	}
	return c
}

// recordingWriter captures prune calls.
type recordingWriter struct {
	games   []string
	players [][]string
	protect []map[string]bool
	keeps   []map[string]bool
	lives   []map[string]bool
}

func (r *recordingWriter) PruneGame(gameID string, players []string, protect map[string]bool) (int, error) {
	sorted := append([]string(nil), players...)
	sort.Strings(sorted)
	r.games = append(r.games, gameID)
	r.players = append(r.players, sorted)
	r.protect = append(r.protect, protect)
	return 0, nil
}

func (r *recordingWriter) PruneExcept(keep, live map[string]bool) (int, error) {
	r.keeps = append(r.keeps, keep)
	r.lives = append(r.lives, live)
	return 0, nil
}

// copyFixture puts the agent's interventions fixture in a temp file.
func copyFixture(t *testing.T) string {
	t.Helper()
	src, err := os.ReadFile(filepath.Join("actions", "testdata", "interventions.json"))
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "interventions.json")
	if err := os.WriteFile(path, src, 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

// perPlayerSerials lists the serials with an agent entry in any per-player flag.
func perPlayerSerials(t *testing.T, path string) []string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Flags map[string]struct {
			Variants map[string]any `json:"variants"`
		} `json:"flags"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	seen := map[string]bool{}
	for _, key := range []string{"player_sensitivity_factor", "player_handicap_factor", "shield_seconds", "partial_shield_seconds", "soft_penalty_action"} {
		for name := range doc.Flags[key].Variants {
			if rest, ok := strings.CutPrefix(name, "agent_"); ok {
				serial, _, _ := strings.Cut(rest, "#")
				seen[serial] = true
			}
		}
	}
	out := make([]string, 0, len(seen))
	for s := range seen {
		out = append(out, s)
	}
	sort.Strings(out)
	return out
}

// UC: UC-040 A23 BR-016
func TestTargetedPrunerOnGameEnd(t *testing.T) {
	t.Run("UC-040/A23 game end prunes the ended game and protects other active games' players", func(t *testing.T) {
		rec := &recordingWriter{}
		parts := &fakeParts{snaps: map[string]gamecontext.GameContext{
			"game_a": gameCtx("game_a", false, "A1", "SHARED"),
			"game_b": gameCtx("game_b", true, "B1", "SHARED"),
			"game_c": gameCtx("game_c", false, "C1"), // ended, not active: not protected
		}}
		p := newTargetedPruner(rec, nil, discardLogger())
		p.parts = parts

		p.OnGameEnd(gameCtx("game_a", false, "A1", "SHARED"))

		if len(rec.games) != 1 || rec.games[0] != "game_a" {
			t.Fatalf("PruneGame games = %v, want [game_a]", rec.games)
		}
		if got := strings.Join(rec.players[0], ","); got != "A1,SHARED" {
			t.Fatalf("players = %s", got)
		}
		if !rec.protect[0]["B1"] || !rec.protect[0]["SHARED"] || rec.protect[0]["C1"] || rec.protect[0]["A1"] {
			t.Fatalf("protect = %v, want the active game_b's players only", rec.protect[0])
		}
	})

	t.Run("UC-040/A23 real Writer and Store: a shadow game's end leaves only the live game's entries", func(t *testing.T) {
		path := copyFixture(t)
		w := actions.NewWriter(path, discardLogger())
		apply := func(gameID, serial string) {
			t.Helper()
			for _, iv := range []string{decision.InterventionGrantShield, decision.InterventionAdjustPlayerSensitivity} {
				if err := w.Apply(context.Background(), decision.Decision{Intervention: iv, TargetSerial: serial, GameID: gameID}); err != nil {
					t.Fatal(err)
				}
			}
		}
		parts := &fakeParts{snaps: map[string]gamecontext.GameContext{}}
		p := newTargetedPruner(w, nil, discardLogger())
		p.parts = parts

		live := gameCtx("game_live", true, "LIVE1")
		parts.snaps["game_live"] = live
		apply("game_live", "LIVE1")
		for i, serial := range []string{"MOCK0001", "MOCK0002", "MOCK0003"} {
			gameID := "game_shadow" + string(rune('a'+i))
			store := gamecontext.NewStore(time.Minute, time.Minute, nil)
			store.OnGameEnd = p.OnGameEnd
			store.SetGameActive(true)
			store.AdoptSessionID(gameID)
			store.SetPlayerIntensity(serial, 0.5)
			snap := store.Snapshot()
			parts.snaps[gameID] = snap
			apply(gameID, serial)
			store.SetGameActive(false) // fires OnGameEnd -> prune
		}
		if got := perPlayerSerials(t, path); strings.Join(got, ",") != "LIVE1" {
			t.Fatalf("per-player serials after 3 shadow games = %v, want [LIVE1]", got)
		}
	})
}

// UC: UC-040 A23 BR-016
func TestTargetedPrunerEvictTicks(t *testing.T) {
	t.Run("UC-040/A23 startup prune waits one settle period, then runs once; later only on retirement", func(t *testing.T) {
		rec := &recordingWriter{}
		parts := &fakeParts{snaps: map[string]gamecontext.GameContext{
			"game_live":                gameCtx("game_live", true, "LIVE1"),
			gamecontext.FallbackGameID: gameCtx("session-4", true, "FB1"),
		}}
		now := time.Unix(1000, 0)
		p := newTargetedPruner(rec, func() time.Duration { return 5 * time.Second }, discardLogger())
		p.parts = parts
		p.start = now
		p.now = func() time.Time { return now }

		p.OnEvictTick([]string{"game_x"}) // before settle: the live games may not have reported yet
		if len(rec.keeps) != 0 {
			t.Fatal("pruned before the settle period")
		}
		now = now.Add(5 * time.Second)
		p.OnEvictTick(nil)
		if len(rec.keeps) != 1 {
			t.Fatalf("startup prune ran %d times, want 1", len(rec.keeps))
		}
		if !rec.keeps[0]["LIVE1"] || !rec.keeps[0]["FB1"] {
			t.Fatalf("keep = %v", rec.keeps[0])
		}
		if !rec.lives[0]["game_live"] || !rec.lives[0]["session-4"] || rec.lives[0][gamecontext.FallbackGameID] {
			t.Fatalf("live games = %v", rec.lives[0])
		}
		p.OnEvictTick(nil)
		if len(rec.keeps) != 1 {
			t.Fatal("pruned on a tick that retired no partition")
		}
		p.OnEvictTick([]string{"game_done"})
		if len(rec.keeps) != 2 {
			t.Fatal("no prune after a partition was retired")
		}
	})
}
