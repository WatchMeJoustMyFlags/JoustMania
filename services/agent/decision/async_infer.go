package decision

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// async_infer.go makes LLM inference ASYNCHRONOUS (#917). Before this, decide()
// called backend.Infer SYNCHRONOUSLY inside OnEvaluate (#739): a 2-10s call blocked
// the per-game Export-handler goroutine for seconds. Here the call is fired in its
// own goroutine and OnEvaluate returns promptly; when the result lands — seconds
// later — it is RE-VALIDATED against the CURRENT game context (the game may have
// moved on) and only then applied, or dropped with a recorded reason.
//
// THE DESIGN FORK (#917 leaves this open; resolved here, called out in the PR):
// "loop never blocks" + "timeout -> rules" + "the system always decides" admit two
// readings of what a fired-but-not-yet-answered cycle does THIS cycle:
//
//	(A) run rules immediately as a stopgap every admitted cycle, and let the async
//	    LLM result land later as an ADDITIONAL decision; or
//	(B) defer to the async result: the admitted cycle decides nothing synchronously,
//	    and the SINGLE decision for that context is produced when inference completes
//	    (re-validated + applied) or, on timeout/error/unparseable, by the rules engine
//	    IN THE ASYNC COMPLETION PATH for that same context.
//
// This file implements (B). It is the reading most consistent with #741's fallback
// chain (timeout/infer-error/unparseable -> rules is a property of the inference
// ATTEMPT, not a parallel rules cycle) and with "one decision per admitted llm
// context": (A) would dispatch two interventions per admitted cycle (a rules one and,
// a cycle later, an llm one) and double-charge the rate limiter. Under (B) the loop
// still never blocks (it returns immediately after firing), the system always decides
// (the async path ALWAYS produces a decision — the model's, or the rules fallback),
// and a timeout cleanly degrades to rules for the current context.
//
// PILE-UP GUARD: with per-game loops (#845) and per-cycle OnEvaluate, the #847 cadence
// floor limits FIRING to ~1/interval — but an async call slower than the interval
// could still overlap the next cycle. The inflight atomic flag (one per Loop = one
// per game) ensures at most ONE Infer is in flight per game: a cycle that finds the
// flag set skips firing (the gate already rate-limited it, so a skipped fire is fine)
// and falls back to rules for that cycle so the game still gets a decision.
//
// LEAK-FREENESS: every fired goroutine is bounded by context.WithTimeout(rootCtx,
// budget) AND tracked on inferWG, so the latency budget caps its lifetime and
// AwaitInflight (shutdown / tests) can join all in-flight goroutines. rootCtx is the
// agent's root context; on shutdown its cancellation unblocks a well-behaved Infer.

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

// asyncInferState is the per-Loop async-inference machinery (#917). It is embedded
// in Loop. The pile-up guard, the wait group, the game id, and the context provider
// all live here so the async path is cohesive and the Loop struct documents the
// async surface in one place.
type asyncInferState struct {
	// gameID identifies which partition this loop serves, so the async apply path
	// re-fetches the RIGHT game's current context from the provider. Set by the
	// factory (SetGameID); empty = the fallback partition (single-game mode).
	gameID string
	// ctxProvider re-fetches the current GameContext at apply time. Nil disables the
	// async path entirely — decide() then keeps the synchronous #739 call (so a Loop
	// built without async wiring, e.g. an old test, is unchanged). main.go injects the
	// Multiplexer-backed provider so production is async.
	ctxProvider ContextProvider
	// rootCtx is the agent's root context; every fired inference goroutine derives its
	// timeout context from it, so shutdown cancellation unblocks in-flight calls. Nil
	// falls back to context.Background (a Loop without an injected root still works).
	rootCtx context.Context
	// inflight is the pile-up guard: true while an Infer is in flight for THIS game.
	// A cycle that finds it set does NOT fire a second call. Atomic so the concurrent
	// trace + metrics Export handlers cannot both fire.
	inflight atomic.Bool
	// inferWG tracks every in-flight inference goroutine so AwaitInflight can join
	// them at shutdown (no goroutine leak) and tests can deterministically wait for an
	// async result to land before asserting.
	inferWG sync.WaitGroup
}

// SetGameID records which partition this loop serves, so the async apply path
// re-fetches the right game's current context (#917). Set by the factory at
// construction, before the loop serves; not safe to call concurrently with
// OnEvaluate.
func (l *Loop) SetGameID(id string) { l.gameID = id }

// SetContextProvider injects the re-validation source for async inference (#917):
// the seam the async apply path reads the CURRENT GameContext from. main.go injects
// the Multiplexer-backed provider so production inference is async; a nil provider
// (the default) keeps decide() on the synchronous #739 path, so any Loop not wired
// for async is behavior-unchanged. Set at construction, before the loop serves.
func (l *Loop) SetContextProvider(p ContextProvider) { l.ctxProvider = p }

// SetRootContext injects the agent's root context (#917). Every fired inference
// goroutine derives its latency-budget context from it, so shutdown cancellation
// unblocks a well-behaved in-flight Infer. Set at construction, before serving.
func (l *Loop) SetRootContext(ctx context.Context) { l.rootCtx = ctx }

// AwaitInflight blocks until every in-flight async inference for this loop has
// completed (#917). main.go calls it during graceful shutdown so no inference
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

// asyncEnabled reports whether this loop is wired for async inference (#917): a
// context provider must be present (the re-validation source). Without it the loop
// keeps the synchronous #739 call, so an un-wired Loop is unchanged.
func (l *Loop) asyncEnabled() bool { return l.ctxProvider != nil }

// fireInferAsync launches ONE inference for this cycle in its own goroutine and
// returns immediately (#917) — the heart of "the loop never blocks". It claims the
// pile-up guard first: if a call is already in flight for this game it returns false
// WITHOUT firing (the caller then falls back to rules for this cycle). On a
// successful claim it captures the fire-time snapshot (model, latency budget, the
// trigger), spawns the goroutine under inferWG, and hands off to runInfer.
//
// The captured snapshot is used ONLY to build the prompt and bound the call; the
// APPLY path re-fetches the current context from the provider, never this snapshot —
// that separation is the whole point of #917.
func (l *Loop) fireInferAsync(snapshot flags.Snapshot, backend Backend, c gamecontext.GameContext, trig EvalTrigger) bool {
	// Pile-up guard: only one Infer in flight per game. CompareAndSwap makes the
	// claim atomic against the concurrent Export handlers — exactly one cycle wins.
	if !l.inflight.CompareAndSwap(false, true) {
		return false
	}

	budget := snapshot.LLMGate.LatencyBudget
	if budget <= 0 {
		// Defense in depth: durationFlag already floors this at the default, but a
		// hand-built snapshot (tests) could pass zero. Never run without a deadline —
		// an unbounded hung Infer would pin the inflight flag and leak the goroutine.
		budget = time.Duration(flags.DefaultLLMLatencyBudgetSeconds * float64(time.Second))
	}

	root := l.rootCtx
	if root == nil {
		root = context.Background()
	}

	l.inferWG.Add(1)
	go func() {
		defer l.inferWG.Done()
		defer l.inflight.Store(false) // release the guard for the next cycle, always
		l.runInfer(root, snapshot, backend, c, trig, budget)
	}()
	return true
}

// runInfer is the body of the fired goroutine (#917): it calls Infer under the
// latency-budget context, times the call, and routes the outcome to the apply path.
// It runs OFF the decision loop, so it may take seconds without blocking anything.
//
// On timeout or infer-error/unparseable it produces NO model decision; the apply
// path then runs the deterministic rules engine for the CURRENT context (the #741
// timeout -> rules chain), so the game always gets a decision. The latency is
// measured fire-to-completion and recorded on the apply trace regardless of outcome.
func (l *Loop) runInfer(root context.Context, snapshot flags.Snapshot, backend Backend, c gamecontext.GameContext, trig EvalTrigger, budget time.Duration) {
	now := l.now
	if now == nil {
		now = time.Now
	}
	start := now()

	// Bound the call by the latency budget. A backend that respects ctx unblocks at
	// the deadline; one that does not still has its RESULT dropped here (we stop
	// waiting on it via inferCtx.Err()), and the goroutine ends when Infer finally
	// returns — bounded in practice because the production endpointBackend errors
	// immediately and the test fakes honor ctx.
	inferCtx, cancel := context.WithTimeout(root, budget)
	defer cancel()

	// M7-2 cross-game context (#929): inject the SAME rolling-window block the sync
	// llmDecide path would, so the PRODUCTION (async) path actually carries the last N
	// game summaries — #917 moved prompt assembly here, so without this the feature
	// would be bypassed in production. renderContextBlock is the shared #951 Loop
	// helper (nil-window-safe -> "", 0), so an un-wired loop is unaffected. count is
	// carried on the outcome and stamped on the agent.llm.infer span at apply time
	// (emitAsyncInferSpan), mirroring how the sync path records AttrLLMContextGames.
	contextBlock, contextCount := l.renderContextBlock(snapshot)
	// M7-3 operator note (#930): inject the SAME validated operator context note the sync
	// llmDecide path would, so the PRODUCTION (async) path actually carries it — #917
	// moved prompt assembly here, so without this the feature would be bypassed in
	// production (the same trap M7-2 hit). present/len are carried on the outcome and
	// stamped on agent.llm.infer at apply time (emitAsyncInferSpan), mirroring the sync
	// path. A rejected note → "" → no operator section.
	contextNote, notePresent, noteLen := resolveContextNote(snapshot)
	prompt := llm.Build(llm.BuildInput{
		Snapshot:     snapshot,
		Context:      c,
		Now:          start,
		ContextBlock: contextBlock,
		ContextNote:  contextNote,
	})

	// The raw inference call (#739). The ATTRIBUTION agent.llm.infer span is still
	// emitted later by applyAsyncResult (it is deliberately backdated to fire start so
	// the apply trace covers fire->apply); we do NOT move it. But that means at THIS
	// point — when the HTTP call actually fires — no infer span is active in ctx, so
	// #1112's traceparent injection (openai.go) would have nothing to parent a litellm
	// gateway's gen_ai span under (it would orphan onto the async-apply root or a
	// parent-less trace). To restore "one trace" on the async/production path (#1096
	// follow-up) we open a MINIMAL, short-lived client span JUST around the outbound
	// call so callCtx carries an active, sampled span for the injector to read. It
	// carries only the request model — no gen_ai attribution duplication; the apply-time
	// agent.llm.infer keeps owning that. Default-safe: with a no-op tracer/propagator
	// this is a no-op and no header is written.
	callCtx, callSpan := l.Tracer.Start(inferCtx, SpanLLMInferCall,
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(attribute.String("gen_ai.request.model", backend.Name())),
	)
	raw, inferErr := backend.Infer(callCtx, prompt)
	callSpan.End()
	end := now()

	l.applyAsyncResult(root, snapshot, trig, asyncOutcome{
		raw:                raw,
		inferErr:           inferErr,
		timedOut:           inferCtx.Err() == context.DeadlineExceeded,
		start:              start,
		end:                end,
		backend:            backend,
		contextGames:       contextCount,
		contextNotePresent: notePresent,
		contextNoteLen:     noteLen,
	})
}

// asyncOutcome bundles the raw result of one fired inference for the apply path
// (#917). It carries the RAW response (decoded at apply time) plus timing, never a
// pre-validated decision: re-validation against the current context is the apply
// path's job.
type asyncOutcome struct {
	// raw is the model's raw response text from Infer; decoded at apply time.
	raw string
	// inferErr is the Infer transport error, if any (incl. a deadline error).
	inferErr error
	// timedOut is true when the latency budget elapsed (DiscardTimeout): the result
	// is dropped outright and the cycle falls back to rules for the current context.
	timedOut bool
	// start/end bound the actual call, for the agent.llm.infer span timestamps and
	// the inference.latency_ms attribute (end.Sub(start)).
	start, end time.Time
	// backend is the tier that was called, for inference.used attribution and the
	// gen_ai.request.model on the infer span.
	backend Backend
	// contextGames is the M7-2 rolling-context-window COUNT actually injected into
	// this call's prompt (#929): the number of recent game summaries rendered into the
	// cross-game narrative block. Carried from runInfer to the apply path so the
	// agent.llm.infer span (emitted at apply time) records AttrLLMContextGames, exactly
	// as the synchronous llmDecide path does. 0 when no window is wired (un-wired loops).
	contextGames int
	// contextNotePresent / contextNoteLen are the M7-3 operator-note view (#930) for the
	// prompt this call actually used: whether a validated operator note was injected and
	// its rune length. Carried from runInfer (validation happens at fire time, on the
	// fire-time snapshot) to the apply path so emitAsyncInferSpan stamps the same
	// bounded present+len view on agent.llm.infer that the sync llmDecide path records.
	// present=false/len=0 when the note was unset, flagd-unreachable, or rejected.
	contextNotePresent bool
	contextNoteLen     int
}
