package gamesummary

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// coordinator.go is the M7-1 game-end glue (#928): it hangs off the same
// gamecontext.Store.OnGameEnd signal the #844 retrospective uses, builds the
// Summary from the pre-reset snapshot, writes it to disk, and records the
// aggregation as an agent.game.summary span. It fires EXACTLY ONCE per game for
// BOTH real and shadow games.
//
// Why a SEPARATE component (not an extension of the retro path): the retro builds
// an LLM PROMPT and is gated by the global LLM budget (#847); the summary builds a
// structured JSON FILE and must run unconditionally for every game (no budget, no
// eligibility). Bolting it onto the retro would entangle the file write with the
// LLM gate — a budget-exhausted game would silently produce no summary, violating
// #928's "after EVERY real and synthetic game". So the summary is its own
// once-per-game component, wired alongside the retro on the SAME store hook (main.go
// chains both), each with its OWN dedupe — they never double-fire each other
// because each owns its own claimed-id set.

// SpanGameSummary is the aggregation span (#928 acceptance: "the aggregation is
// visible as a span in the trace"). Dot-notation per the house convention; named
// distinctly from agent.llm.retro so the two game-end emissions are independently
// greppable in Jaeger.
const SpanGameSummary = "agent.game.summary"

// Span attribute keys for agent.game.summary. Low-cardinality counts + ids only
// (per .claude/rules: keep span attributes low-cardinality) — the full narrative
// lives in the on-disk file, not the span.
const (
	attrGameID            = "game.id"
	attrGameKind          = "game.kind"
	attrPlayerCount       = "summary.player_count"
	attrEliminationCount  = "summary.elimination_count"
	attrInterventionCount = "summary.intervention_count"
	attrEngagementPoints  = "summary.engagement_points"
	attrDominated         = "summary.dominated"
	attrPath              = "summary.path"
)

// dedupeWindow bounds the per-game claimed-id LRU. It mirrors the retro's bounded
// dedupe (retro_capture.go): concurrent game-ends (#845) interleave, so a single
// last-id is insufficient — an LRU of recent ids suppresses a repeated end for any
// remembered game. 8 comfortably exceeds GAME_MAX_CONCURRENT_GAMES=4.
const dedupeWindow = 8

// Coordinator builds + persists one summary per game on the store's OnGameEnd
// signal (#928). It is wired to EVERY gamecontext.Store partition's OnGameEnd in
// main(), so one coordinator dedupes across all partitions. Safe for concurrent
// use: per-partition ends are each serialized by their store, but concurrent
// partitions may fire simultaneously, so the dedupe state is mutex-guarded.
type Coordinator struct {
	writer *Writer
	tracer trace.Tracer
	log    *slog.Logger
	// now is injectable for tests; nil uses time.Now. It stamps Summary.GeneratedAt
	// only — the aggregation itself is clock-free.
	now func() time.Time

	mu sync.Mutex
	// claimed is the set of recently summarized game ids; claimedOrder is the LRU
	// eviction order (oldest first). Bounded by dedupeWindow.
	claimed      map[string]struct{}
	claimedOrder []string
}

// NewCoordinator builds a Coordinator writing summaries via w (use NewWriter with
// DirFromEnv() in main). log nil uses slog.Default().
func NewCoordinator(w *Writer, log *slog.Logger) *Coordinator {
	if log == nil {
		log = slog.Default()
	}
	return &Coordinator{
		writer:  w,
		tracer:  otel.Tracer(instrumentationName),
		log:     log,
		now:     time.Now,
		claimed: make(map[string]struct{}),
	}
}

// instrumentationName scopes the coordinator's tracer, matching the agent module
// convention used by the decision package.
const instrumentationName = "github.com/joustmania/agent"

// OnGameEnd is the gamecontext.Store.OnGameEnd callback (#928). It builds + writes
// the game summary exactly once per game, for both real and shadow games. It is
// defensive, mirroring the retro:
//   - skips if the snapshot still reports GameActive (only end-of-game summarizes);
//   - skips if SessionID is empty (no game_id to key the file on);
//   - skips if SessionID was already summarized (exactly-once per game).
//
// The aggregation runs under an agent.game.summary span carrying the key counts.
// A file-write error marks the span as errored and logs, but never panics — a
// failed summary must not take the observation path down.
func (c *Coordinator) OnGameEnd(gc gamecontext.GameContext) {
	if gc.Session.GameActive != nil && *gc.Session.GameActive {
		return // not actually a game end
	}
	if gc.SessionID == "" {
		return
	}
	if !c.claimGame(gc.SessionID) {
		return // already summarized this game
	}

	now := c.now
	if now == nil {
		now = time.Now
	}

	// The span IS the audit of the aggregation (#928 acceptance). It is a standalone
	// root: there is no inbound RPC context at game end (same as the retro span).
	_, span := c.tracer.Start(context.Background(), SpanGameSummary)
	defer span.End()

	summary := BuildSummary(gc, BuildOptions{Now: now()})

	span.SetAttributes(
		attribute.String(attrGameID, summary.GameID),
		attribute.String(attrGameKind, summary.GameKind),
		attribute.Int(attrPlayerCount, summary.PlayerCount),
		attribute.Int(attrEliminationCount, summary.EliminationCount),
		attribute.Int(attrInterventionCount, summary.InterventionCount),
		attribute.Int(attrEngagementPoints, len(summary.EngagementTrend)),
		attribute.Bool(attrDominated, summary.Dominance.Dominated),
		attribute.String(attrPath, c.writer.Path(summary.GameID)),
	)

	if err := c.writer.Write(summary); err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, "summary write failed")
		c.log.Error("agent.game.summary_write_failed",
			"game_id", summary.GameID,
			"game_kind", summary.GameKind,
			"error", err)
		return
	}

	c.log.Info("agent.game.summary_written",
		"game_id", summary.GameID,
		"game_kind", summary.GameKind,
		"players", summary.PlayerCount,
		"eliminations", summary.EliminationCount,
		"interventions", summary.InterventionCount,
		"dominated", summary.Dominance.Dominated,
		"path", c.writer.Path(summary.GameID))
}

// claimGame records gameID as summarized and reports whether THIS call won the
// claim (the game had not been summarized yet). Mutex-guarded for exactly-once.
// Remembers the last dedupeWindow ids (LRU) so interleaved concurrent ends (#845)
// each summarize exactly once.
func (c *Coordinator) claimGame(gameID string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, ok := c.claimed[gameID]; ok {
		return false
	}
	c.claimed[gameID] = struct{}{}
	c.claimedOrder = append(c.claimedOrder, gameID)
	if len(c.claimedOrder) > dedupeWindow {
		oldest := c.claimedOrder[0]
		c.claimedOrder = c.claimedOrder[1:]
		delete(c.claimed, oldest)
	}
	return true
}

// compile-time guard: Coordinator.OnGameEnd matches the store hook signature.
var _ func(gamecontext.GameContext) = (*Coordinator)(nil).OnGameEnd
