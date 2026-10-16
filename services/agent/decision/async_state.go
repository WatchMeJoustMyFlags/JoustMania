package decision

import (
	"context"
	"sync"
	"sync/atomic"

	"github.com/joustmania/agent/gamecontext"
)

// async_state.go is the per-Loop async machinery the remote decide path
// (decide_hook.go, #1226) and the intervention-effect sampler (effect.go, #918)
// share: the per-game pile-up guard, the in-flight wait group, the game id, the
// re-validation context provider and the agent root context. It was introduced for
// the #917 async LLM path, which #1234 retired; decision.backend is now the only
// in-game LLM route and it runs on exactly this machinery.

// ContextProvider yields the CURRENT GameContext for a game_id at apply time — the
// re-validation source (#917). It is the Multiplexer's Snapshot, adapted: the async
// apply path must read the game's state AS IT IS NOW (post-inference), not the
// snapshot captured when the call fired, because the 2-10s in flight may have ended
// the game, eliminated the target, or changed the flags. *gamecontext.Multiplexer
// satisfies it via MuxContextProvider; tests supply a fake that can flip state
// between fire and apply to drive each discard reason deterministically.
type ContextProvider interface {
	// Current returns the live GameContext for gameID and whether that partition
	// still exists. A false ok means the partition was evicted (the game ended past
	// grace) — there is no current context, so an in-flight result is stale.
	Current(gameID string) (gamecontext.GameContext, bool)
}

// MuxContextProvider adapts a *gamecontext.Multiplexer to ContextProvider so the
// async apply path can re-fetch the current partition snapshot by game_id. It is a
// thin pass-through to Snapshot; kept in the decision package (not gamecontext) so
// the Multiplexer carries no decision-layer dependency.
type MuxContextProvider struct {
	Mux interface {
		Snapshot(gameID string) (gamecontext.GameContext, bool)
	}
}

// Current re-reads the live partition snapshot for gameID (#917 re-validation
// source). A non-existent partition returns ok=false, which the apply path maps to
// DiscardStaleContext.
func (p MuxContextProvider) Current(gameID string) (gamecontext.GameContext, bool) {
	if p.Mux == nil {
		return gamecontext.GameContext{}, false
	}
	return p.Mux.Snapshot(gameID)
}

// asyncInferState is the per-Loop async machinery (#917, now driving the remote
// decide path #1226). It is embedded
// in Loop. The pile-up guard, the wait group, the game id, and the context provider
// all live here so the async path is cohesive and the Loop struct documents the
// async surface in one place.
type asyncInferState struct {
	// gameID identifies which partition this loop serves, so a landing remote answer
	// re-fetches the RIGHT game's current context from the provider. Set by the
	// factory (SetGameID); empty = the fallback partition (single-game mode).
	gameID string
	// ctxProvider re-fetches the current GameContext when a remote answer lands. Nil
	// disables the async path: a remote decision.backend then falls back to rules
	// (decide.fallback_reason=async_not_wired). main.go injects the Multiplexer-backed
	// provider so production is async.
	ctxProvider ContextProvider
	// rootCtx is the agent's root context; every fired remote-call goroutine derives its
	// timeout context from it, so shutdown cancellation unblocks in-flight calls. Nil
	// falls back to context.Background (a Loop without an injected root still works).
	rootCtx context.Context
	// inflight is the pile-up guard: true while a remote call is in flight for THIS game.
	// A cycle that finds it set does NOT fire a second call. Atomic so the concurrent
	// trace + metrics Export handlers cannot both fire.
	inflight atomic.Bool
	// inferWG tracks every in-flight remote-call goroutine so AwaitInflight can join
	// them at shutdown (no goroutine leak) and tests can deterministically wait for an
	// async result to land before asserting.
	inferWG sync.WaitGroup
}

// SetGameID records which partition this loop serves, so a landing remote answer
// re-fetches the right game's current context (#917). Set by the factory at
// construction, before the loop serves; not safe to call concurrently with
// OnEvaluate.
func (l *Loop) SetGameID(id string) { l.gameID = id }

// SetContextProvider injects the re-validation source (#917): the seam a landing
// remote answer (and the #918 effect sampler) reads the CURRENT GameContext from.
// main.go injects the Multiplexer-backed provider; a nil provider (the default)
// makes a remote backend fall back to rules. Set at construction, before the loop
// serves.
func (l *Loop) SetContextProvider(p ContextProvider) { l.ctxProvider = p }

// SetRootContext injects the agent's root context (#917). Every fired remote-call
// goroutine derives its timeout context from it, so shutdown cancellation unblocks
// a well-behaved in-flight call. Set at construction, before serving.
func (l *Loop) SetRootContext(ctx context.Context) { l.rootCtx = ctx }

// AwaitInflight blocks until every in-flight remote decide call for this loop has
// completed (#917). main.go calls it during graceful shutdown so no such
// goroutine outlives the process; tests call it to make the async result land
// deterministically before asserting (no real sleeps). It is safe to call when
// nothing is in flight (returns immediately).
func (l *Loop) AwaitInflight() {
	l.inferWG.Wait()
	// Also join the #918 intervention-effect follow-up samplers: they are the loop's
	// other background goroutines and must not outlive the process (goleak, #923). Their
	// timers are bounded by rootCtx, so a shutdown-cancelled sample returns promptly.
	l.effectWG.Wait()
}

// asyncEnabled reports whether this loop is wired for async remote calls (#917): a
// context provider must be present (the re-validation source). Without it a remote
// backend falls back to rules.
func (l *Loop) asyncEnabled() bool { return l.ctxProvider != nil }

// gameActive reports whether the current context's game is still in progress — the
// game-level re-validation gate for a result that lands asynchronously. A nil/false
// GameActive means the game ended while the call was in flight.
func gameActive(c gamecontext.GameContext) bool {
	return c.Session.GameActive != nil && *c.Session.GameActive
}

// targetPresent reports whether a player-targeted decision's target is still alive/
// connected in the current context. A target absent from the players map, or
// present but no longer Active, has left during the call (eliminated/disconnected).
func targetPresent(c gamecontext.GameContext, serial string) bool {
	p := c.Players[serial]
	if p == nil {
		return false
	}
	// Active nil means "never observed" — treat as present (do not over-discard on an
	// unlabeled signal), matching the sync path's lenient treatment of unknown state.
	return p.Active == nil || *p.Active
}
