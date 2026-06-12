package decision

import (
	"sync"

	"github.com/joustmania/agent/gamecontext"
)

// LoopSet manages one decision Loop per game partition (#845 PR C), lazily
// created on first touch. It is the per-game counterpart to the GameContext
// Multiplexer: where the Multiplexer gives each concurrent game its own Store
// (own players / session / elimination order), the LoopSet gives each game its
// own decision Loop — and therefore its OWN weighted rate-limit budget, its OWN
// log/span throttle slot, and its OWN per-cycle LayerState. Two concurrent games
// can no longer draw down a single shared per-minute budget or race each other's
// throttle: A exhausting max_interventions_per_minute leaves B unaffected.
//
// CRITICAL — per-game engine isolation: the factory MUST construct a FRESH rules
// engine per Loop. The objective-weighted engine (*ObjectiveRules) carries
// per-cycle mutable state that the loop drives every evaluation:
//
//   - SetObjectives / SetFitness publish this cycle's flag values into the
//     engine's LiveObjectives / LiveFitness sources BEFORE Evaluate runs;
//   - LastFitness is read back AFTER Evaluate to lift fitness.evaluated onto the
//     span;
//   - the engine also retains lastEval / lastDifficultyAt / lastFitness.
//
// If two games' Loops shared one engine, game A's SetObjectives could land
// between game B's SetObjectives and B's Evaluate, so B would score against A's
// objective weights and the LastFitness B reads could be A's — the spans would
// carry the wrong agent.objectives / fitness.evaluated for the game. A fresh
// engine per Loop (mirroring main.go's NewObjectiveRulesLive wiring) removes the
// shared mutable state entirely, so the loops never contend.
//
// The action sink, by contrast, is safe to SHARE across Loops: actions.Writer
// holds no per-game state — it serializes a read-modify-write of the flagd
// interventions file under its own mutex, keyed by the decision's target. The
// factory may therefore close over one sink. The flag source and tracer are
// likewise stateless/shareable.
//
// LoopSet is safe for concurrent use: For (lazy create) and Retain (eviction)
// run from different goroutines than the Export handlers that call For.
type LoopSet struct {
	mu      sync.Mutex
	loops   map[string]*Loop
	factory func(gameID string) *Loop
}

// NewLoopSet builds a LoopSet over the given per-game Loop factory. The factory
// is invoked once per game_id, the first time that partition is evaluated; see
// main.go for the production wiring (fresh engine per loop, shared sink).
func NewLoopSet(factory func(gameID string) *Loop) *LoopSet {
	return &LoopSet{
		loops:   make(map[string]*Loop),
		factory: factory,
	}
}

// For returns the Loop for gameID, creating it lazily via the factory on first
// touch. A recreated loop (after Retain dropped it) starts with a fresh budget
// and throttle state — acceptable because a partition is only dropped once its
// game has fully ended past grace; a resumed game begins a new budget window.
func (ls *LoopSet) For(gameID string) *Loop {
	ls.mu.Lock()
	defer ls.mu.Unlock()
	if l := ls.loops[gameID]; l != nil {
		return l
	}
	l := ls.factory(gameID)
	ls.loops[gameID] = l
	return l
}

// Retain drops every Loop whose gameID is NOT in active, releasing that game's
// budget and throttle state along with its partition. active is the current set
// of live partitions (mux.Partitions() as a set); it is called right after the
// Multiplexer's EvictStale so a removed partition's Loop is dropped in lockstep
// with its Store. A dropped game's Loop is recreated lazily (fresh budget) if
// the game resumes.
//
// The fallback partition's Loop is NEVER dropped, mirroring the Multiplexer's
// fallback Store which is never wholesale-removed: in single-game mode every
// signal lands on the fallback loop, and dropping it mid-session would reset the
// budget under a live game.
func (ls *LoopSet) Retain(active map[string]struct{}) {
	ls.mu.Lock()
	defer ls.mu.Unlock()
	for id := range ls.loops {
		if id == gamecontext.FallbackGameID {
			continue // fallback loop is permanent (single-game zero-regression)
		}
		if _, ok := active[id]; !ok {
			// Dropping a loop with an async inference still in flight (#917) is safe
			// and self-healing: the goroutine remains bounded by rootCtx+budget and
			// terminates on its own, and its apply path re-fetches the context, finds
			// the partition gone (Current()->ok=false), and discards as stale_context
			// — no decision is applied to an ended game. AwaitInflight only joins
			// loops still in the map, so a dropped loop's goroutine simply isn't waited
			// on; it cannot outlive the budget regardless.
			delete(ls.loops, id)
		}
	}
}

// Len reports the number of live Loops (including the fallback loop when
// present). Used by tests and for observability.
func (ls *LoopSet) Len() int {
	ls.mu.Lock()
	defer ls.mu.Unlock()
	return len(ls.loops)
}

// AwaitInflight blocks until every live Loop's in-flight async inference has
// completed (#917). main.go calls it during graceful shutdown so no fired
// inference goroutine outlives the process. The Loops' fired goroutines are already
// bounded by the agent root context (cancelled on signal) so a well-behaved Infer
// returns promptly; this join makes the wait explicit and leak-free. It snapshots
// the loop pointers under the lock, then waits OUTSIDE it so a concurrent For/Retain
// is never blocked behind a slow inference.
func (ls *LoopSet) AwaitInflight() {
	ls.mu.Lock()
	loops := make([]*Loop, 0, len(ls.loops))
	for _, l := range ls.loops {
		loops = append(loops, l)
	}
	ls.mu.Unlock()
	for _, l := range loops {
		l.AwaitInflight()
	}
}
