package decision

import (
	"context"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/llm"
)

// async_apply.go is the APPLY-when-done half of async inference (#917): it runs in
// the fired goroutine when Infer returns (or times out), re-validates the result
// against the CURRENT game context, and applies or discards it — recording the
// latency and the applied/discarded outcome on a dedicated agent.llm.apply trace.
//
// THE CRITICAL INVARIANT (#917): re-validation reads the LATEST context, fetched
// fresh from the ContextProvider at apply time — NOT the snapshot captured when the
// call fired. The 2-10s in flight may have ended the game, eliminated the target,
// tightened the allow-list, or filled the rate budget; every one of those is a
// reason to DROP a result that was perfectly valid when fired. The SAME permission
// chain a synchronous decision runs (evaluatePermission: allow-list + battery +
// weighted rate limit) is re-run here at apply time, so a now-disallowed or
// now-unaffordable action is blocked exactly like a sync one.
//
// BUDGET COHERENCE with #847: the global llmBudget was already charged at FIRE time
// (admission, in gateLLM). The per-game weighted rate limiter is charged HERE at
// apply time, through runDecision -> evaluatePermission, exactly as for a sync
// decision — so neither budget is double-charged nor skipped.

// applyAsyncResult is the apply path (#917). It builds the agent.llm.apply root
// (backdated to fire start so the trace spans fire->apply), records the inference
// latency, decodes the response, and then either:
//   - drops the result with a recorded inference.discarded_reason (stale context,
//     game ended, target gone, permission revoked, rate limited, or timeout), or
//   - applies it through the SAME runDecision permission chain a sync decision uses.
//
// On timeout / infer-error / unparseable it falls back to the deterministic rules
// engine FOR THE CURRENT CONTEXT — the #741 timeout->rules chain, so the rules
// engine is always CONSULTED (subject to its own evalInterval cadence; see
// fallbackToRules) rather than the loop silently dropping the cycle.
func (l *Loop) applyAsyncResult(root context.Context, snapshot flags.Snapshot, trig EvalTrigger, out asyncOutcome) {
	// Build the apply audit root. It is backdated to the trigger T0 (the fire cycle's
	// Export arrival) so the trace duration covers the whole fire->infer->apply span,
	// the same backdating runDecision's root uses. It carries the game id so a Jaeger
	// query by game.id finds the async decision alongside the sync ones.
	// #1140 (Slice A): the apply root is parent-less by the #917 non-blocking design.
	// Link it back to the fire cycle's originating agent.signal_received trace (carried
	// as a SpanContext value on the outcome) so the apply trace — and the agent.decision/
	// agent.action spans nested under it — is navigable from the signal that caused it.
	// No-op when out.fireSC is invalid (the production default), exactly like gameTraceLink.
	// #1195 (refining #1178): also start it as a remote CHILD of the gameplay_phase
	// span — the async-materialized decision/action are part of the game's active play,
	// so they live INSIDE the game trace nested under gameplay_phase. The fire-cycle
	// Link is KEPT alongside (navigates to the originating signal cycle); gameplay_phase
	// is the new parent. Two-tier fallback (inGameRemoteParent): gameplay_phase span_id
	// -> game ROOT span_id (#1187) -> own root + fire-cycle Link, all carried on the
	// outcome from fire time, so this never loses correlation.
	applyParent := root
	if parentCtx, ok := inGameRemoteParent(root, out.gameTraceID, out.gameTraceSpanID, out.gameplayPhaseSpanID); ok {
		applyParent = parentCtx
	}
	applyOpts := []trace.SpanStartOption{
		trace.WithTimestamp(trig.T0),
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(
			attribute.String(AttrSessionID, l.gameID),
			attribute.String(AttrGameID, l.gameID),
			attribute.Int64(AttrLLMLatencyMs, out.latencyMs()),
		),
	}
	applyOpts = append(applyOpts, fireCycleLink(out.fireSC)...)
	applyCtx, applyRoot := l.Tracer.Start(applyParent, SpanLLMApply, applyOpts...)
	defer applyRoot.End()

	// Emit the agent.llm.infer call span as a child of the apply root, with the REAL
	// call timestamps. It mirrors the synchronous #739 infer span (gen_ai.* request
	// attribution + the model's reasoning), but parented here because the fire-cycle
	// root is long gone. Decoding happens inside so the span carries the error on the
	// failure path exactly as the sync path did.
	decisions, fallback := l.emitAsyncInferSpan(applyCtx, out)

	// Re-fetch the CURRENT context — the re-validation source. A missing partition
	// (evicted because the game ended past grace) is the stalest possible result.
	current, ok := l.ctxProvider.Current(l.gameID)
	if !ok {
		l.discard(applyRoot, DiscardStaleContext, out)
		return
	}

	// Timeout / infer-error / unparseable: no usable model output. Fall back to the
	// rules engine for the CURRENT context so the game still gets a decision (#741).
	// The result itself is recorded as discarded (timeout vs the infer fallback
	// reason), then the rules decision is applied through the normal chain.
	if out.timedOut {
		// Latency budget elapsed: drop outright, rules decide for the current context.
		l.fallbackToRules(applyCtx, applyRoot, snapshot, current, out, DiscardTimeout)
		return
	}
	if decisions == nil {
		// A clean noop decodes to zero decisions with an EMPTY fallback (the model was
		// heard, chose nothing) — that is NOT a discard. Only a non-empty fallback
		// (FallbackUnparseable) means the answer was unusable and rules must decide.
		if fallback != "" {
			l.fallbackToRules(applyCtx, applyRoot, snapshot, current, out, FallbackUnparseable)
			return
		}
		// Clean noop: nothing to apply, nothing to discard. The apply root + infer span
		// already recorded the latency and the model's reasoning.
		applyRoot.SetAttributes(attribute.Bool("inference.applied", false))
		return
	}

	// Re-validate the game itself: it must still be active. A game that ended while
	// the call was in flight makes any intervention meaningless.
	if !gameActive(current) {
		l.discard(applyRoot, DiscardGameEnded, out)
		return
	}

	// Build the per-cycle LayerState for the apply spans from the fire-time snapshot,
	// but stamp the RESOLVED tier as inference.used (it answered) so the async
	// decision span attributes match a sync llm decision.
	state := newLayerState(snapshot)
	state.InferenceUsed = out.backend.Name()
	state.LLMFallbackReason = ""

	// Apply each decision through the SAME chain a sync decision uses, but with a
	// per-decision target re-validation in front (the target may have been eliminated
	// or disconnected during the call). evaluatePermission inside runDecision re-runs
	// the allow-list + battery + weighted rate limit at apply time.
	for _, d := range decisions {
		if d.TargetSerial != "" && !targetPresent(current, d.TargetSerial) {
			l.discard(applyRoot, DiscardTargetGone, out)
			continue
		}
		l.applyOneAsync(applyCtx, applyRoot, snapshot, current, d, &state, out)
	}
}

// emitAsyncInferSpan emits the agent.llm.infer span for the async call (#917),
// decodes the raw response, and returns the parsed decisions + fallback reason —
// the same contract llmDecide returns synchronously. The span carries the real
// call timestamps and, on success, the model's reasoning; on an error/unparseable
// response it carries AttrLLMInferError and the latency.
func (l *Loop) emitAsyncInferSpan(ctx context.Context, out asyncOutcome) ([]Decision, string) {
	_, span := l.Tracer.Start(ctx, SpanLLMInfer,
		trace.WithTimestamp(out.start),
		trace.WithAttributes(semconvGenAIChat(out.backend.Name())...),
	)
	// session.id + game.id (#1088): the async inference span is attributable to its
	// game like its agent.llm.apply parent and the synchronous infer span.
	span.SetAttributes(
		attribute.String(AttrSessionID, l.gameID),
		attribute.String(AttrGameID, l.gameID),
	)
	span.SetAttributes(attribute.Int64(AttrLLMLatencyMs, out.latencyMs()))
	// M7-2 (#929): record the cross-game context-window count actually injected into
	// the prompt on the REAL inference span, mirroring the synchronous llmDecide path
	// (AttrLLMContextGames). out.contextGames is what runInfer rendered (0 = un-wired).
	span.SetAttributes(attribute.Int(AttrLLMContextGames, out.contextGames))
	// M7-3 (#930): the operator-note view (present + rune length) on the REAL inference
	// span, mirroring the synchronous llmDecide path. out.contextNote* is what runInfer
	// validated + injected at fire time (present=false/len=0 = no/rejected note).
	span.SetAttributes(
		attribute.Bool(AttrLLMContextNotePresent, out.contextNotePresent),
		attribute.Int(AttrLLMContextNoteLen, out.contextNoteLen),
	)
	defer span.End(trace.WithTimestamp(out.end))

	if out.timedOut {
		// #1206: surface the timeout as span STATUS=Error (not just an attribute) so the
		// fail-open degrade-to-rules path is visible in Jaeger's error view. inferErr
		// carries the deadline error when present; otherwise record the budget cause.
		const timeoutMsg = "latency budget exceeded"
		span.SetAttributes(attribute.String(AttrLLMInferError, timeoutMsg))
		if out.inferErr != nil {
			span.RecordError(out.inferErr)
			span.SetAttributes(semconv.ErrorType(out.inferErr))
		}
		span.SetStatus(codes.Error, timeoutMsg)
		return nil, FallbackUnparseable
	}
	if out.inferErr != nil {
		// #1206: transport/infer failure is an ERROR on the span, mirroring litellm's
		// otel.status_code=ERROR. Control flow unchanged — still degrades to rules.
		span.SetAttributes(attribute.String(AttrLLMInferError, out.inferErr.Error()))
		span.RecordError(out.inferErr)
		span.SetAttributes(semconv.ErrorType(out.inferErr))
		span.SetStatus(codes.Error, out.inferErr.Error())
		l.Log.Warn("agent.llm.infer_failed", "session_id", l.gameID, "tier", out.backend.Name(), "error", out.inferErr)
		return nil, FallbackUnparseable
	}
	resp, err := llm.Decode(out.raw)
	if err != nil {
		// #1206: an unparseable response is an ERROR on the span, not just parse_ok=false.
		// Control flow unchanged — still degrades to rules (FallbackUnparseable).
		span.SetAttributes(attribute.String(AttrLLMInferError, err.Error()))
		span.RecordError(err)
		span.SetAttributes(semconv.ErrorType(err))
		span.SetStatus(codes.Error, err.Error())
		l.Log.Warn("agent.llm.unparseable", "session_id", l.gameID, "tier", out.backend.Name(), "error", err)
		return nil, FallbackUnparseable
	}
	span.SetAttributes(
		attribute.String(AttrDecisionReason, resp.Reason),
		attribute.String(AttrDecisionObjective, resp.ObjectiveServed),
		attribute.String(AttrDecisionAction, resp.Intervention),
	)
	if resp.IsNoop() {
		return nil, "" // a valid, contract-following "do nothing": not a fallback
	}
	return []Decision{{
		Intervention:    resp.Intervention,
		TargetSerial:    resp.TargetSerial,
		Value:           resp.Value,
		Reason:          resp.Reason,
		ObjectiveServed: resp.ObjectiveServed,
	}}, ""
}

// applyOneAsync runs one re-validated model decision through the normal permission
// chain (#917). It calls runDecision EXACTLY ONCE — the single authoritative
// evaluatePermission (allow-list + battery + weighted rate limit) at APPLY time, and
// the single charge of the per-game weighted rate limiter (apply-time charging keeps
// #847 budget coherence — no double charge). runDecision emits the agent.decision ->
// agent.action spans and dispatches through the action sink, the SAME path a sync
// decision takes, so the audit shape is identical.
//
// After it runs, the just-appended outcome tells us whether the apply-time
// re-validation blocked the action; if so its BlockReason is mapped onto the #917
// discard vocabulary (permission_revoked / rate_limited) and stamped on the apply
// root, so a now-disallowed or now-unaffordable result is attributable as a
// re-validation failure. A clean dispatch stamps inference.applied=true.
func (l *Loop) applyOneAsync(ctx context.Context, applyRoot trace.Span, snapshot flags.Snapshot, current gamecontext.GameContext, d Decision, state *LayerState, out asyncOutcome) {
	before := len(state.Outcomes)
	l.runDecision(ctx, snapshot, current, d, l.nowOrDefault()(), state)
	if len(state.Outcomes) <= before {
		return // defensive: runDecision always appends one outcome
	}
	o := state.Outcomes[len(state.Outcomes)-1]
	if o.Dispatched {
		applyRoot.SetAttributes(attribute.Bool("inference.applied", true))
		return
	}
	// Blocked at apply time by the re-run permission chain (allow-list/battery ->
	// permission_revoked, rate limit -> rate_limited). The blocked decision span is
	// already audited by runDecision; record the discard reason on the apply root.
	applyRoot.SetAttributes(attribute.String(AttrLLMDiscardedReason, discardForBlock(o.BlockReason)))
	l.Log.Info("agent.llm.discarded",
		"session_id", l.gameID,
		"reason", discardForBlock(o.BlockReason),
		"tier", out.backend.Name(),
		"latency_ms", out.latencyMs(),
	)
}

// fallbackToRules runs the deterministic rules engine for the CURRENT context and
// applies its decisions when the async LLM result could not be used (#917): a
// timeout, or an infer-error / unparseable response. This is the #741
// timeout->rules chain executed in the async completion path, so the rules engine
// is CONSULTED for the current context even though the loop never blocked. Note it
// is "consulted", not "always emits": ObjectiveRules carries its own ~1s
// evalInterval throttle, so if the synchronous loop ran rules <1s before this
// timeout lands, Evaluate returns no decision this round — the engine's intended
// cadence, not a dropped decision.
//
// cause is what made the LLM result unusable: DiscardTimeout (budget elapsed,
// recorded as the discard reason) or FallbackUnparseable (the tier answered, but
// unusably — recorded as inference.fallback_reason, NOT a #917 discard since the
// result was never stale, just bad). Either way the rules decision is applied below.
func (l *Loop) fallbackToRules(ctx context.Context, applyRoot trace.Span, snapshot flags.Snapshot, current gamecontext.GameContext, out asyncOutcome, cause string) {
	state := newLayerState(snapshot)
	state.InferenceUsed = InferenceRules
	if cause == DiscardTimeout {
		// Timeout: the result was dropped outright (#917 discard). inference.used drops
		// to rules with no_backend_available — the tier did not answer in budget.
		applyRoot.SetAttributes(attribute.String(AttrLLMDiscardedReason, DiscardTimeout))
		state.LLMFallbackReason = FallbackNoBackend
	} else {
		// Unparseable/errored: the tier was CALLED (inference.used stays the tier on the
		// infer span), but the answer could not dispatch — rules decide with the
		// unparseable fallback reason, exactly the synchronous #739 contract.
		state.InferenceUsed = out.backend.Name()
		state.LLMFallbackReason = FallbackUnparseable
	}

	// The game must still be active for a rules decision to be worth applying.
	if !gameActive(current) {
		applyRoot.SetAttributes(attribute.String(AttrLLMDiscardedReason, DiscardGameEnded))
		return
	}

	now := l.nowOrDefault()
	for _, d := range l.Rules.Evaluate(ctx, current) {
		l.runDecision(ctx, snapshot, current, d, now(), &state)
	}
}

// discard records a dropped async result on the apply root with its reason and the
// latency (#917). No decision spans are emitted — the result never became a
// decision. It is the single sink for the re-validation-failure paths (stale
// context, game ended, target gone).
func (l *Loop) discard(applyRoot trace.Span, reason string, out asyncOutcome) {
	applyRoot.SetAttributes(
		attribute.String(AttrLLMDiscardedReason, reason),
		attribute.Bool("inference.applied", false),
	)
	l.Log.Info("agent.llm.discarded",
		"session_id", l.gameID,
		"reason", reason,
		"tier", out.backend.Name(),
		"latency_ms", out.latencyMs(),
	)
}

// discardForBlock maps a permission-chain BlockReason onto the #917 discard
// vocabulary so an apply-time re-validation failure is attributable in the issue's
// terms: a rate-limit block is rate_limited; an allow-list or battery block is
// permission_revoked (the action the model chose is no longer permitted).
func discardForBlock(reason BlockReason) string {
	if reason == ReasonRateLimit {
		return DiscardRateLimited
	}
	return DiscardPermissionRevoked
}

// gameActive reports whether the current context's game is still in progress — the
// game-level re-validation gate (#917). A nil/false GameActive means the game ended
// while the inference was in flight.
func gameActive(c gamecontext.GameContext) bool {
	return c.Session.GameActive != nil && *c.Session.GameActive
}

// targetPresent reports whether a player-targeted decision's target is still alive/
// connected in the current context (#917). A target absent from the players map, or
// present but no longer Active, has left during the call (eliminated/disconnected) —
// the decision is stale for that player.
func targetPresent(c gamecontext.GameContext, serial string) bool {
	p := c.Players[serial]
	if p == nil {
		return false
	}
	// Active nil means "never observed" — treat as present (do not over-discard on an
	// unlabeled signal), matching the sync path's lenient treatment of unknown state.
	return p.Active == nil || *p.Active
}

// latencyMs renders the fire-to-completion latency in integer milliseconds for the
// inference.latency_ms attribute (#917).
func (o asyncOutcome) latencyMs() int64 { return o.end.Sub(o.start).Milliseconds() }

// nowOrDefault returns the loop's injected clock, or time.Now when unset.
func (l *Loop) nowOrDefault() func() time.Time {
	if l.now != nil {
		return l.now
	}
	return time.Now
}
