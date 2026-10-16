package main

import (
	"log/slog"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// targetedPruner keeps the per-player intervention flags bounded by the live
// games (UC-040 A23 / BR-016, #1323). It prunes a game's per-player entries when
// the game ends (the store's OnGameEnd hook), when the eviction loop retires a
// partition, and once after startup. The Writer owns the file I/O and the
// per-serial owner records; this type only decides when to prune and which
// players are live.
type targetedPruner struct {
	writer prunerWriter
	parts  partitionSource
	log    *slog.Logger

	// settle is how long after startup the first liveness prune waits, so the
	// live games have reported their players before anything is judged stale
	// (one lifecycle.player_ttl_seconds, read live).
	settle func() time.Duration
	now    func() time.Time
	start  time.Time

	mu      sync.Mutex
	settled bool
}

// prunerWriter is the Writer's prune surface (actions.Writer in production).
type prunerWriter interface {
	PruneGame(gameID string, players []string, protect map[string]bool) (int, error)
	PruneExcept(keep, liveGames map[string]bool) (int, error)
}

// partitionSource is the Multiplexer's read surface.
type partitionSource interface {
	Partitions() []string
	Snapshot(gameID string) (gamecontext.GameContext, bool)
}

func newTargetedPruner(writer prunerWriter, settle func() time.Duration, log *slog.Logger) *targetedPruner {
	if log == nil {
		log = slog.Default()
	}
	return &targetedPruner{writer: writer, settle: settle, log: log, now: time.Now, start: time.Now()}
}

// OnGameEnd prunes the ended game's per-player entries (A23 step 1). Players of
// other active games are protected when the Writer has no owner record for
// them. Chained onto every partition's OnGameEnd.
func (p *targetedPruner) OnGameEnd(c gamecontext.GameContext) {
	if p == nil || p.writer == nil {
		return
	}
	players := make([]string, 0, len(c.Players))
	for serial := range c.Players {
		players = append(players, serial)
	}
	protect := map[string]bool{}
	for _, snap := range p.snapshots() {
		if snap.SessionID == c.SessionID {
			continue
		}
		if snap.Session.GameActive != nil && *snap.Session.GameActive {
			for serial := range snap.Players {
				protect[serial] = true
			}
		}
	}
	n, err := p.writer.PruneGame(c.SessionID, players, protect)
	p.report("game_end", c.SessionID, n, err)
}

// OnEvictTick runs after each eviction tick with the partitions it removed. The
// first tick at least one settle period after startup prunes every entry that
// belongs to no live game (A23 step 2, agent restart); later ticks do so only
// when a partition was retired.
func (p *targetedPruner) OnEvictTick(removed []string) {
	if p == nil || p.writer == nil {
		return
	}
	p.mu.Lock()
	if !p.settled {
		if p.now().Sub(p.start) < p.settleDelay() {
			p.mu.Unlock()
			return
		}
		p.settled = true
		p.mu.Unlock()
		p.reconcile("startup")
		return
	}
	p.mu.Unlock()
	if len(removed) > 0 {
		p.reconcile("partition_retired")
	}
}

// reconcile prunes every per-player entry whose player is in no partition and
// whose owner game is not a partition.
func (p *targetedPruner) reconcile(reason string) {
	keep := map[string]bool{}
	live := map[string]bool{}
	for id, snap := range p.snapshotsByID() {
		if id != gamecontext.FallbackGameID {
			live[id] = true
		}
		if snap.SessionID != "" {
			live[snap.SessionID] = true
		}
		for serial := range snap.Players {
			keep[serial] = true
		}
	}
	n, err := p.writer.PruneExcept(keep, live)
	p.report(reason, "", n, err)
}

func (p *targetedPruner) settleDelay() time.Duration {
	if p.settle == nil {
		return 0
	}
	return p.settle()
}

func (p *targetedPruner) snapshots() []gamecontext.GameContext {
	byID := p.snapshotsByID()
	out := make([]gamecontext.GameContext, 0, len(byID))
	for _, snap := range byID {
		out = append(out, snap)
	}
	return out
}

func (p *targetedPruner) snapshotsByID() map[string]gamecontext.GameContext {
	out := map[string]gamecontext.GameContext{}
	if p.parts == nil {
		return out
	}
	for _, id := range p.parts.Partitions() {
		if snap, ok := p.parts.Snapshot(id); ok {
			out[id] = snap
		}
	}
	return out
}

func (p *targetedPruner) report(reason, gameID string, removed int, err error) {
	if err != nil {
		p.log.Error("agent.targeted_prune_failed", "reason", reason, "game_id", gameID, "error", err)
		return
	}
	if removed > 0 {
		p.log.Info("agent.targeted_pruned", "reason", reason, "game_id", gameID, "serials", removed)
	}
}
