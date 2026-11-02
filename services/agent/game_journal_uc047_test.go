package main

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// The agent wires the journal into the game-end chain: a real game end through
// the chain reaches the experiments root on disk, a shadow one does not.
// UC: UC-047 main step 1 BR-009
func TestGameJournalWiring_RealGameEndReachesDisk(t *testing.T) {
	root := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	rec := newGameJournal(ctx, root, nil, metricnoopProvider(), slog.Default())
	chain := buildGameEndChain(newGameEndStaleCounter(metricnoopProvider()), nil, nil, nil, nil, nil, rec.OnGameEnd)

	active := false
	end := func(id, kind string) gamecontext.GameContext {
		return gamecontext.GameContext{SessionID: id, GameKind: kind, EndReason: gamecontext.EndReasonZero,
			Session: gamecontext.SessionSignals{GameActive: &active}}
	}
	chain(end("game_shadow", gamecontext.GameKindShadow))
	chain(end("game_real", gamecontext.GameKindReal))

	path := filepath.Join(root, "calibration", "real_games.jsonl")
	deadline := time.Now().Add(5 * time.Second)
	for {
		data, err := os.ReadFile(path)
		if err == nil && strings.Contains(string(data), "game_real") {
			if strings.Contains(string(data), "game_shadow") {
				t.Fatalf("shadow game journaled: %s", data)
			}
			if n := strings.Count(string(data), "\n"); n != 1 {
				t.Fatalf("journal has %d lines, want 1", n)
			}
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("real game end did not reach %s: %v", path, err)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
