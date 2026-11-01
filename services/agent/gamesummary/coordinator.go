package gamesummary

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// coordinator.go is the M7-1 game-end glue (#928): it hangs off the same
// gamecontext.Store.OnGameEnd signal the #844 retrospective uses, builds the
// Summary from the pre-reset snapshot, hands it to the observer (the in-memory
// cross-game window: real games feed the real-game window, shadow games their
// experiment's linked set, UC-042 BR-005), and records the
// aggregation as an agent.game.summary span. It fires EXACTLY ONCE per game for
// BOTH real and shadow games. Nothing is written to disk: the per-game summary
// file writer was removed by #1368 (nothing read it, and it kept per-controller
// arcs without bound).
//
// Why a SEPARATE component (not an extension of the retro path): the retro builds
// an LLM PROMPT and is gated by the global LLM budget (#847); the summary must
// run unconditionally for every game (no budget, no eligibility). Bolting it onto
// the retro would entangle it with the LLM gate — a budget-exhausted game would silently produce no summary, violating
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
// (per .claude/rules: keep span attributes low-cardinality).
const (
	attrGameID               = "game.id"
	attrGameKind             = "game.kind"
	attrPlayerCount          = "summary.player_count"
	attrEliminationCount     = "summary.elimination_count"
	attrInterventionsApplied = "summary.interventions_applied"
	attrInterventionsBlocked = "summary.interventions_blocked"
	attrEngagementPoints     = "summary.engagement_points"
	attrDominated            = "summary.dominated"
	// summary.predictions.* (UC-042 A12, #2479): set only when the game had scored
	// predictions.
	attrPredictionsScored     = "summary.predictions.scored"
	attrPredictionsHit        = "summary.predictions.hit"
	attrPredictionsMiss       = "summary.predictions.miss"
	attrPredictionsUnscorable = "summary.predictions.unscorable"
	// counterfactual and controls are kept out of the four above (BR-034).
	attrPredictionsCounterfactual = "summary.predictions.counterfactual"
	attrPredictionsControls       = "summary.predictions.controls"
	// summary.goal.* (UC-042 A12, #2479): set only when the game had a goal;
	// attainment only when a term could be measured.
	attrGoalProfile    = "summary.goal.profile"
	attrGoalSource     = "summary.goal.source"
	attrGoalAttainment = "summary.goal.attainment"
	// attrGameTraceID / attrGameTraceSpanID are the canonical game-session-trace Link
	// attribute keys (#1133/#1157), matching decision.AttrGameTraceID /
	// AttrGameTraceSpanID verbatim so the summary's hub Link (#1174) carries the SAME
	// attribute names every other agent spoke does. Defined locally (not imported from
	// decision) because decision imports gamesummary — an import cycle the other way.
	attrGameTraceID     = "game.trace_id"
	attrGameTraceSpanID = "game.trace_span_id"
)

// dedupeWindow bounds the per-game claimed-id LRU. It mirrors the retro's bounded
// dedupe (retro_capture.go): concurrent game-ends (#845) interleave, so a single
// last-id is insufficient — an LRU of recent ids suppresses a repeated end for any
// remembered game. 8 comfortably exceeds GAME_MAX_CONCURRENT_GAMES=4.
const dedupeWindow = 8

// Observer is notified of each game Summary the moment it is built: the M7-2
// rolling-window seam (#929). The window store (gamewindow.Store) satisfies it; it
// files a real game in the real-game window and a shadow game in its experiment's
// linked set, and drops every per-controller field (UC-042 BR-004/BR-005).
// It is OPTIONAL (nil = only the span is emitted). Called once per game (inside
// the dedupe), for BOTH real and shadow games.
type Observer interface {
	// Record ingests one freshly-built game Summary. It must not block (the window
	// store's Record is a mutex-guarded append) — it runs on the game-end path.
	Record(Summary)
}

// Coordinator builds one summary per game on the store's OnGameEnd
// signal (#928). It is wired to EVERY gamecontext.Store partition's OnGameEnd in
// main(), so one coordinator dedupes across all partitions. Safe for concurrent
// use: per-partition ends are each serialized by their store, but concurrent
// partitions may fire simultaneously, so the dedupe state is mutex-guarded.
type Coordinator struct {
	tracer trace.Tracer
	log    *slog.Logger
	// observer, when non-nil, is notified of each built Summary (#929 M7-2): the
	// rolling context-window store hangs off here so the window advances on every
	// game end. Set once at construction via SetObserver, before the coordinator
	// serves, so no lock is needed around the read.
	observer Observer
	// experimentParent resolves the agent.experiment ROOT span's SpanContext by
	// experiment_id (#1188), reading the registry's ExperimentSpanContext. When wired
	// and the finished game is experiment-bound with a live root span, the
	// agent.game.summary span is re-parented as a remote CHILD of the experiment span
	// (joining the experiment's trace) instead of the pre-#1188 game-LINKED own-root
	// span. The game Link is retained so the summary stays navigable to its game too. A
	// nil seam (the default / tests), a non-experiment game, or a torn-down experiment
	// (ok=false) falls back to the existing behavior. Set once at construction via
	// SetExperimentParent, before the coordinator serves, so no lock is needed.
	experimentParent func(experimentID string) (trace.SpanContext, bool)
	// now is injectable for tests; nil uses time.Now. It stamps Summary.GeneratedAt
	// only — the aggregation itself is clock-free.
	now func() time.Time
	// predictions returns the game's finalized scored predictions (the Book's
	// lookup, #2479). Set once at construction via SetPredictionLookup; nil = none.
	predictions func(gameID string) []PredictionOutcome
	// goal returns the game's goal attainment (the Book's lookup, #2479). Set once
	// at construction via SetGoalLookup; nil = none.
	goal func(gameID string) *Goal

	mu sync.Mutex
	// claimed is the set of recently summarized game ids; claimedOrder is the LRU
	// eviction order (oldest first). Bounded by dedupeWindow.
	claimed      map[string]struct{}
	claimedOrder []string
}

// NewCoordinator builds a Coordinator. log nil uses slog.Default().
func NewCoordinator(log *slog.Logger) *Coordinator {
	if log == nil {
		log = slog.Default()
	}
	return &Coordinator{
		tracer:  otel.Tracer(instrumentationName),
		log:     log,
		now:     time.Now,
		claimed: make(map[string]struct{}),
	}
}

// SetObserver wires the M7-2 rolling-window observer (#929): main.go constructs
// ONE gamewindow.Store (the global cross-game memory, shared across all per-game
// loops like the LLM budget) and registers it here, so the window
// advances on every game end. Optional and additive — a coordinator with no
// observer is exactly the M7-1 behavior. Set during construction, before the
// store fires OnGameEnd, so the field read in OnGameEnd needs no lock.
func (c *Coordinator) SetObserver(o Observer) { c.observer = o }

// SetPredictionLookup wires the scored-prediction lookup (#2479, UC-042 A12). The
// prediction Book finalizes a game's results BEFORE this coordinator runs (it sits
// earlier in the game-end chain). Set during construction, before the store fires
// OnGameEnd, so the field read needs no lock.
func (c *Coordinator) SetPredictionLookup(f func(gameID string) []PredictionOutcome) {
	c.predictions = f
}

// SetGoalLookup wires the goal-attainment lookup (#2479, UC-042 A12). The Book
// scores a game's goal earlier in the game-end chain. Set during construction.
func (c *Coordinator) SetGoalLookup(f func(gameID string) *Goal) { c.goal = f }

// SetExperimentParent wires the experiment ROOT-span SpanContext lookup seam (#1188):
// a func returning the agent.experiment span's SpanContext for an experiment_id
// (reading the registry's ExperimentSpanContext) and whether one is available. When
// wired and the finished game is experiment-bound with a live root span, the
// agent.game.summary span is re-parented as a remote CHILD of the experiment span. A
// nil seam (the default), a non-experiment game, or a torn-down experiment falls back
// to the pre-#1188 game-linked own-root span. Set during construction, before the
// store fires OnGameEnd, so the field read needs no lock.
func (c *Coordinator) SetExperimentParent(f func(experimentID string) (trace.SpanContext, bool)) {
	c.experimentParent = f
}

// instrumentationName scopes the coordinator's tracer, matching the agent module
// convention used by the decision package.
const instrumentationName = "github.com/joustmania/agent"

// OnGameEnd is the gamecontext.Store.OnGameEnd callback (#928). It builds
// the game summary exactly once per game, for both real and shadow games. It is
// defensive, mirroring the retro:
//   - skips if the snapshot still reports GameActive (only end-of-game summarizes);
//   - skips if SessionID is empty (no game_id to dedupe on);
//   - skips if SessionID was already summarized (exactly-once per game).
//
// The aggregation runs under an agent.game.summary span carrying the key counts.
//
// UC-041 BR-002: summaries are not durable. Nothing is written to disk here; the
// aggregates live only in the in-memory window (Observer) and are lost on restart.
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
	//
	// Hub-and-spoke correlation (#1174): Link this per-game outcome span back to the
	// game-session trace (the hub) via the SHARED gamecontext.TraceLink primitive — the
	// same Link logic retro + decision + experiment.* carry (decision.GameTraceLink
	// delegates to it) — so "show me everything for game X" lists the summary among the
	// spokes. The ids come from the OnGameEnd GameContext (#1133 correlation gauge).
	// nil-safe: empty/invalid ids => no Link, no error.
	startOpts := gamecontext.TraceLink(gc.GameTraceID, gc.GameTraceSpanID, attrGameTraceID, attrGameTraceSpanID)
	// #1188: re-parent the summary under the experiment ROOT span for an
	// experiment-bound game (the experiment outlives its games, so its per-game summary
	// is a timeline-correct child). The game Link above is retained. A non-experiment
	// game / unwired seam / torn-down experiment yields context.Background() — the
	// pre-#1188 own-root behavior, unchanged.
	parent := c.summaryParentCtx(gc.ExperimentID)
	_, span := c.tracer.Start(parent, SpanGameSummary, startOpts...)
	defer span.End()

	opts := BuildOptions{Now: now()}
	if c.predictions != nil {
		opts.Predictions = c.predictions(gc.SessionID)
	}
	if c.goal != nil {
		opts.Goal = c.goal(gc.SessionID)
	}
	summary := BuildSummary(gc, opts)

	// Advance the M7-2 rolling context window (#929); skipped when no observer is wired.
	if c.observer != nil {
		c.observer.Record(summary)
	}

	span.SetAttributes(
		attribute.String(attrGameID, summary.GameID),
		attribute.String(attrGameKind, summary.GameKind),
		attribute.Int(attrPlayerCount, summary.PlayerCount),
		attribute.Int(attrEliminationCount, summary.EliminationCount),
		attribute.Int(attrInterventionsApplied, summary.InterventionsApplied),
		attribute.Int(attrInterventionsBlocked, summary.InterventionsBlocked),
		attribute.Int(attrEngagementPoints, len(summary.EngagementTrend)),
		attribute.Bool(attrDominated, summary.Dominance.Dominated),
	)
	if len(summary.Predictions) > 0 {
		n := CountPredictions(summary.Predictions)
		span.SetAttributes(
			attribute.Int(attrPredictionsScored, n.Scored),
			attribute.Int(attrPredictionsHit, n.Hit),
			attribute.Int(attrPredictionsMiss, n.Miss),
			attribute.Int(attrPredictionsUnscorable, n.Unscorable),
			attribute.Int(attrPredictionsCounterfactual, n.Counterfactual),
			attribute.Int(attrPredictionsControls, n.Controls),
		)
	}
	goalProfile, goalAttainment := "", -1.0
	if g := summary.Goal; g != nil {
		goalProfile = g.Profile
		// Same values as goal.SourceNamed / goal.SourceFromObjectives; goal imports
		// gamesummary, so the constants cannot be imported here without a cycle.
		source := "named"
		if g.FromObjectives {
			source = "from_objectives"
		}
		span.SetAttributes(attribute.String(attrGoalProfile, g.Profile), attribute.String(attrGoalSource, source))
		if g.Attainment != nil {
			goalAttainment = *g.Attainment
			span.SetAttributes(attribute.Float64(attrGoalAttainment, *g.Attainment))
		}
	}

	c.log.Info("agent.game.summary_built",
		"game_id", summary.GameID,
		"game_kind", summary.GameKind,
		"players", summary.PlayerCount,
		"eliminations", summary.EliminationCount,
		"interventions_applied", summary.InterventionsApplied,
		"interventions_blocked", summary.InterventionsBlocked,
		"dominated", summary.Dominance.Dominated,
		"predictions_scored", CountPredictions(summary.Predictions).Scored,
		"predictions_hit", CountPredictions(summary.Predictions).Hit,
		"predictions_miss", CountPredictions(summary.Predictions).Miss,
		"predictions_unscorable", CountPredictions(summary.Predictions).Unscorable,
		"predictions_counterfactual", CountPredictions(summary.Predictions).Counterfactual,
		"predictions_controls", CountPredictions(summary.Predictions).Controls,
		"goal_profile", goalProfile,
		"goal_attainment", goalAttainment)
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

// summaryParentCtx returns the context the agent.game.summary span is started from
// (#1188): a context whose remote parent is the experiment ROOT span for an
// experiment-bound game with a live root span, else plain context.Background() (the
// pre-#1188 own-root behavior). Reads the SpanContext via the injected experimentParent
// seam and installs it with gamecontext.RemoteParentFromSpanContext.
//
// GRACEFUL FALLBACK: an empty experiment_id, a nil seam, or ok=false all yield
// context.Background(), so the summary span is unchanged from before #1188 and emission
// is never broken.
func (c *Coordinator) summaryParentCtx(experimentID string) context.Context {
	base := context.Background()
	if experimentID == "" || c.experimentParent == nil {
		return base
	}
	sc, ok := c.experimentParent(experimentID)
	if !ok {
		return base
	}
	ctx, _ := gamecontext.RemoteParentFromSpanContext(base, sc)
	return ctx
}

// compile-time guard: Coordinator.OnGameEnd matches the store hook signature.
var _ func(gamecontext.GameContext) = (*Coordinator)(nil).OnGameEnd
