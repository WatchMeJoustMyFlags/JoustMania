package decision

import (
	"context"
	"time"

	"go.opentelemetry.io/otel/attribute"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamewindow"
	"github.com/joustmania/agent/llm"
)

// semconvGenAIChat is the GenAI request attribution for the inference span: the
// operation (chat) and the request model. It mirrors the capture span's gen_ai.*
// attributes (llmPromptAttributes) so a real call and a captured prompt are
// queryable the same way in Jaeger; the JSON output type is implied by the
// RESPONSE CONTRACT and asserted by the decoder, so it is not re-emitted here.
func semconvGenAIChat(model string) []attribute.KeyValue {
	return []attribute.KeyValue{
		semconv.GenAIOperationNameChat,
		semconv.GenAIRequestModel(model),
		genAIOutputTypeJSON,
	}
}

// llm_decide.go is the actual LLM decision path (#739): the agent.mode=="llm"
// branch that, when the #847 gate ADMITS and resolve_backend (#741) resolves a
// REACHABLE non-rules tier, asks that tier's model for a decision instead of
// running the deterministic rules engine. It is the M4 milestone's payoff — the
// agent finally USES the inference tier the resolver has been honestly attributing
// since #741, rather than always falling back to rules.
//
// It is deliberately thin and SAFE, reusing the existing machinery so the LLM path
// is indistinguishable from the rules path everywhere it matters:
//
//   - PROMPT: llm.Build (already objective-aware via the snapshot — it renders the
//     objective weights and the allow-list into the System prompt) is reused
//     verbatim, the SAME builder the prompt-capture span uses. Objectives shape
//     what the model is asked to optimize (endurance/balanced/accelerate/chaos).
//   - PARSE: llm.Decode (decode.go) parses the response DEFENSIVELY. Unparseable,
//     invalid, or out-of-vocabulary output NEVER dispatches an arbitrary action —
//     it returns no decision and decide() falls back to the rules engine with
//     FallbackUnparseable recorded on the span.
//   - GATE + LIMIT: the parsed Decision is returned to decide() and flows through
//     the EXACT SAME runDecision -> evaluatePermission chain as a rules decision,
//     so the interventions.allowed allow-list, the battery threshold, and the
//     weighted rate limit all apply IDENTICALLY. An LLM-chosen action not in
//     interventions.allowed is blocked with decision.blocked=true — there is no
//     LLM-specific dispatch path to bypass any gate.
//
// This synchronous path is now the COMPATIBILITY fallback (#739): #917 made the
// production call ASYNCHRONOUS (async_infer.go / async_apply.go fire Infer off the
// loop and re-validate the result against the current context before applying).
// decide() takes this synchronous path ONLY when async is not wired (no context
// provider injected) — e.g. a Loop built by an older test — so llmDecide stays the
// behavior-compatible synchronous reference implementation.

// llmDecide runs one inference against the resolved backend and returns the parsed
// decisions plus the fallback_reason to record. The contract decide() relies on:
//
//   - (decisions, "")            : the model produced a valid, in-vocab decision
//     (possibly a noop, which yields ZERO decisions but is NOT a fallback — the
//     model legitimately chose to do nothing). inference.used stays the called
//     tier; no fallback_reason.
//   - (nil, FallbackUnparseable) : Infer errored, or the response was empty /
//     not JSON / missing a required field / named an unknown objective. The caller
//     MUST fall back to the rules engine; inference.used stays the called tier
//     (the tier answered, just unusably) and FallbackUnparseable rides the span.
//
// llmDecide NEVER returns a decision built from untrusted output: a parse failure
// yields nil, so decide() runs rules instead. It does NOT itself enforce the
// allow-list — that is runDecision/evaluatePermission's job, applied uniformly to
// whatever decision is ultimately dispatched, so an out-of-allow-list LLM choice
// is blocked exactly like an out-of-allow-list rules choice (decision.blocked).
func (l *Loop) llmDecide(ctx context.Context, backend Backend, snapshot flags.Snapshot, c gamecontext.GameContext) ([]Decision, string) {
	now := l.now
	if now == nil {
		now = time.Now
	}

	// M7-2 cross-game context (#929): pull the last N recent game summaries from the
	// shared rolling window and render them into the prompt's narrative context block,
	// so the model reasons across games/sessions — not just this game's live snapshot.
	// N is the LIVE flag (snapshot.ContextGames), clamped to the window's retention
	// cap; contextCount is how many summaries were ACTUALLY injected (bounded by how
	// many games have ended), recorded on the spans below. A nil window (rules-only /
	// unwired / tests) yields an empty block and a 0 count — purely additive.
	contextBlock, contextCount := l.renderContextBlock(snapshot)

	// Reuse the objective-aware prompt builder (the same one the capture span uses).
	// The System prompt already encodes the objective weights and the allow-list, so
	// the model is asked to optimize for THIS session's objectives.
	prompt := llm.Build(llm.BuildInput{
		Snapshot:     snapshot,
		Context:      c,
		Now:          now(),
		ContextBlock: contextBlock,
	})

	// Record the inference as a gen_ai.* child span so the audit trace shows the
	// actual call (#739): the request model and, on success, the reasoning the model
	// returned. The span is the SAME shape the capture path uses for attribution, but
	// it represents a REAL call (Infer ran), not a captured-but-unsent prompt.
	//
	// gen_ai.request.model is the tier ACTUALLY called (backend.Name()), NOT the
	// configured agent.model: on a degraded chain (configured "claude" unreachable ->
	// served by "phi4-mini") the request really went to the lower tier, and the span
	// must say so to match the sibling decision span's inference.used. The capture
	// span legitimately keeps the configured model; this is a real request.
	//
	// agent.llm.context_games (#929) is recorded here on the REAL inference span with
	// the COUNT actually injected — what the model truly saw, not the raw flag.
	infCtx, span := l.Tracer.Start(ctx, SpanLLMInfer, trace.WithAttributes(
		append(semconvGenAIChat(backend.Name()),
			attribute.Int(AttrLLMContextGames, contextCount))...,
	))
	defer span.End()

	// TODO(#738/#742): bound this call with a per-call context.WithTimeout once a real
	// transport lands — today the production endpointBackend.Infer returns immediately
	// (sentinel error) and the fakes are instant, but a slow network Infer would block
	// this Export-handler goroutine (async application is #917).
	raw, err := backend.Infer(infCtx, prompt)
	if err != nil {
		// A reachable tier that cannot actually answer (the unwired production
		// endpointBackend, a transport error, a timeout) degrades to rules SAFELY —
		// no untrusted output, no dispatch. Recorded, never silent.
		span.SetAttributes(attribute.String(AttrLLMInferError, err.Error()))
		l.Log.Warn("agent.llm.infer_failed",
			"session_id", c.SessionID,
			"tier", backend.Name(),
			"error", err)
		return nil, FallbackUnparseable
	}

	resp, err := llm.Decode(raw)
	if err != nil {
		// Unparseable / invalid / out-of-vocab: the cardinal safety case. The model
		// answered, but the answer cannot be trusted to dispatch — fall back to rules.
		span.SetAttributes(attribute.String(AttrLLMInferError, err.Error()))
		l.Log.Warn("agent.llm.unparseable",
			"session_id", c.SessionID,
			"tier", backend.Name(),
			"error", err)
		return nil, FallbackUnparseable
	}

	// Record the model's reasoning + chosen objective on the inference span (the
	// "reasoning behind the action", #739 acceptance). decision.* attribution then
	// rides the agent.decision span via runDecision exactly as for a rules decision.
	span.SetAttributes(
		attribute.String(AttrDecisionReason, resp.Reason),
		attribute.String(AttrDecisionObjective, resp.ObjectiveServed),
		attribute.String(AttrDecisionAction, resp.Intervention),
	)

	// A noop is a valid, contract-following choice that dispatches nothing: the
	// model decided, and decided to do nothing. Return ZERO decisions (no rules
	// fallback — the model was heard) with no fallback_reason.
	if resp.IsNoop() {
		return nil, ""
	}

	// Map the validated response onto a Decision. The allow-list is NOT checked
	// here — runDecision/evaluatePermission applies it (and the battery + rate-limit
	// gates) uniformly, so an out-of-allow-list LLM intervention is blocked with
	// decision.blocked=true exactly like a rules one. Objectives carries the cycle's
	// weights so the decision span's agent.objectives is consistent with the rules
	// path; Fitness is left nil (the model does not emit fitness inputs).
	return []Decision{{
		Intervention:    resp.Intervention,
		TargetSerial:    resp.TargetSerial,
		Value:           resp.Value,
		Reason:          resp.Reason,
		ObjectiveServed: resp.ObjectiveServed,
		Objectives:      snapshot.Objectives,
	}}, ""
}

// renderContextBlock pulls the last N recent game summaries from the shared M7-2
// rolling window and renders the cross-game NARRATIVE CONTEXT BLOCK (#929),
// returning the block plus the COUNT actually injected. N is the LIVE flag
// (snapshot.ContextGames), clamped to [0, gamewindow.RetentionCap] so a
// mis-/over-configured flag can never ask for more than the window can ever hold;
// the window itself further bounds the result by how many games have actually
// ended (Store.Recent(N) returns all it holds when N exceeds its length). The
// returned count is len(Recent), i.e. what the prompt + the span attribute report —
// what the model truly saw, never the raw flag.
//
// A nil window (no SetContextWindow — rules-only deployments and most tests) renders
// no block and reports 0, so the prompt carries no PRIOR GAMES section: M7-2 is
// purely additive over the #739 path. When a window IS wired, gamewindow.Render
// always returns a non-empty block (it renders "(no prior games)" for an empty
// window), so the section is present on every llm call (acceptance: "each LLM call
// includes the last N game summaries as a narrative context block").
func (l *Loop) renderContextBlock(snapshot flags.Snapshot) (block string, count int) {
	if l.contextWindow == nil {
		return "", 0
	}
	n := snapshot.ContextGames
	if n < 0 {
		n = 0
	}
	if n > gamewindow.RetentionCap {
		n = gamewindow.RetentionCap
	}
	recent := l.contextWindow.Recent(n)
	return gamewindow.Render(recent), len(recent)
}
