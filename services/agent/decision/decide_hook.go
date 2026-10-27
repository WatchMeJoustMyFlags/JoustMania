package decision

import (
	"context"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	otelmetric "go.opentelemetry.io/otel/metric"
	semconv "go.opentelemetry.io/otel/semconv/v1.41.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// decide_hook.go wires the decide contract (epic #1225) into the decision loop
// (#1226). decision.backend selects who decides:
//
//   - rules (default): the loop keeps calling the rules engine exactly as before —
//     ObjectiveRules.EvaluateDecide is Evaluate plus the decide answers from the
//     SAME ranking (one rankLocked call, one chaos RNG draw per evaluation), so the
//     emitted decisions are byte-for-byte unchanged. The answers ride an
//     agent.decide span; like every other span on the sync path it is emitted only
//     on a cycle that produces decisions (idle cycles stay span-free).
//   - anything else: the remote backend OWNS the decision. The call runs off the
//     loop on the existing async machinery (inflight CAS, inferWG, rootCtx,
//     ContextProvider re-read at landing), bounded by the backend's timeout flag.
//     While a call is in flight for a game the rules engine does NOT also act for
//     it. An error, timeout or invalid response (decide.Validate) falls back to the
//     rules engine for the CURRENT context, recorded as decide.fallback_reason.
//
// Every remote call (and every rules decision) is recorded on one agent.decide
// span: OTel GenAI semantic conventions where a name exists
// (gen_ai.agent.name, gen_ai.conversation.id, gen_ai.request.model, error.type,
// and a gen_ai.evaluation.result event for the confidence-threshold judgement),
// decide.* attributes for the rest.

// SpanDecide is the per-decision decide-contract span (#1226). On the remote path
// it is the root of the async trace (a remote child of the game's game_cycle span
// when known, else gameplay_phase / the game root, UC-040 BR-013; Linked to the fire
// cycle) and wraps the backend call — the
// backend's own `chat {model}` CLIENT span nests under it — and the resulting
// agent.decision -> agent.action spans. On the rules path it is a sibling of the
// agent.decision spans under agent.signal_received, so the existing trace shape is
// unchanged.
const SpanDecide = "agent.decide"

// agent.decide span attributes without a GenAI semconv name (#1226).
const (
	// AttrDecideBackend is the backend that produced the answers ("rules" after a
	// fallback).
	AttrDecideBackend = "decide.backend"
	// AttrDecideBackendRequested is the decision.backend flag value this cycle.
	AttrDecideBackendRequested = "decide.backend.requested"
	// AttrDecideFallbackReason is why rules decided although another backend was
	// requested ("" when the requested backend decided).
	AttrDecideFallbackReason = "decide.fallback_reason"
	// AttrDecideStateDigest is decide.State.Digest. It changes on every snapshot
	// (the narrative includes time): use it to correlate traces, not as a cache key.
	AttrDecideStateDigest = "decide.state.digest"
	// AttrDecideQuestions / AttrDecideAnswers are size-capped JSON renderings of the
	// questions (id, kind, options, range; the prose text is omitted) and answers.
	AttrDecideQuestions = "decide.questions"
	AttrDecideAnswers   = "decide.answers"
	// AttrDecideConfidence is the routing confidence: the ffa.intervention answer's.
	AttrDecideConfidence = "decide.confidence"
	// AttrDecideConfidenceMin is the minimum confidence over all answers.
	AttrDecideConfidenceMin = "decide.confidence.min"
	// AttrDecideActThreshold is the EFFECTIVE act threshold as read when the answer
	// landed: the answering backend's own decision.<backend>.act_confidence_threshold
	// when it has one (jev, #1287), else decision.act_confidence_threshold. Present
	// only when the confidence router ran (outcome act or held): the rules path and
	// rules fallbacks do not route by confidence.
	AttrDecideActThreshold = "decide.act_confidence_threshold"
	// AttrDecideActThresholdSource names the flag AttrDecideActThreshold came from:
	// "jev" (decision.jev.act_confidence_threshold) or "global"
	// (decision.act_confidence_threshold). Present exactly when the threshold is.
	AttrDecideActThresholdSource = "decide.act_confidence_threshold.source"
	// AttrDecideLatencyMs is the backend call latency (ranking time for rules).
	AttrDecideLatencyMs = "decide.latency_ms"
	// AttrDecideOutcome is one of the DecideOutcome* values.
	AttrDecideOutcome = "decide.outcome"
	// AttrDecideRationale is the backend's one-line rationale, truncated.
	AttrDecideRationale = "decide.rationale"
	// AttrDecideInvalidReason names the cross-question inconsistency behind
	// outcome=invalid.
	AttrDecideInvalidReason = "decide.invalid_reason"
	// AttrDecideDiscardedReason names why nothing acted although an answer exists:
	// stale_context / game_ended / target_gone (re-validation against the current
	// context), kill_switch / backend_changed (live flags at landing) or shutdown.
	// It accompanies outcome=discarded, and can also appear with outcome=fallback
	// when the rules fallback itself found the game gone or ended.
	AttrDecideDiscardedReason = "decide.discarded_reason"
	// AttrDecideBlocked / AttrDecideBlockReason (#1295) report what the permission
	// chain did with the decision the answers describe (the cycle's first emitted
	// decision): blocked true/false, and the BlockReason when blocked. Present
	// exactly when that decision went through the permission chain (outcome act, or
	// a rules fallback that emitted a decision); decide.outcome stays the routing
	// outcome.
	AttrDecideBlocked     = "decide.blocked"
	AttrDecideBlockReason = "decide.block_reason"
)

// decide.outcome values.
const (
	// DecideOutcomeAct: the answer was routed to the permission chain (runDecision);
	// whether a gate then blocked it is on the agent.decision child.
	DecideOutcomeAct = "act"
	// DecideOutcomeHeld: a valid intervention answer below the act threshold; no action.
	DecideOutcomeHeld = "held"
	// DecideOutcomeNoop: the backend chose noop; no action.
	DecideOutcomeNoop = "noop"
	// DecideOutcomeInvalid: the answers were individually valid but inconsistent
	// across questions (e.g. noop with a target); no action.
	DecideOutcomeInvalid = "invalid"
	// DecideOutcomeFallback: the requested backend could not decide; rules did.
	DecideOutcomeFallback = "fallback"
	// DecideOutcomeDiscarded: a valid act answer that no longer fit the CURRENT
	// context when it landed (game over, target gone); no action.
	DecideOutcomeDiscarded = "discarded"
)

// Evaluation recorded as a gen_ai.evaluation.result event on agent.decide.
const (
	evaluationEventName    = "gen_ai.evaluation.result"
	evaluationActThreshold = "act_confidence_threshold"
	evaluationLabelPass    = "pass"
	evaluationLabelFail    = "fail"
)

// Size caps for model-supplied or unbounded strings on spans and decisions.
const (
	maxDecideJSONBytes  = 4096
	maxRationaleRunes   = 256
	truncatedJSONMarker = "...(truncated)"
)

// decideEngine is the optional rules-engine seam the hook uses: Evaluate plus the
// decide answers from one ranking. *ObjectiveRules satisfies it; Noop/Probe/test
// engines do not, and then the loop calls plain Evaluate and emits no span.
type decideEngine interface {
	EvaluateDecide(ctx context.Context, c gamecontext.GameContext, build func(charged map[string]bool) decide.Request) ([]Decision, decide.Request, decide.Response, bool)
}

// actedRecorder is the optional seam that arms the rules engine's variance
// cooldown for a decision another backend made (*ObjectiveRules.RecordActed).
type actedRecorder interface {
	RecordActed(Decision)
}

// SetDecideRegistry injects the SHARED decision-backend registry (#1226). main.go
// builds one and calls this on every per-game loop. A nil registry (the default)
// knows no remote backend, so any decision.backend other than rules falls back to
// rules with unknown_backend. Set at construction, before the loop serves.
func (l *Loop) SetDecideRegistry(r *DecideRegistry) { l.decideBackends = r }

// decideRecord is everything the agent.decide span reports for one decision.
type decideRecord struct {
	requested      string
	used           string
	fallbackReason string
	model          string
	req            decide.Request
	resp           decide.Response
	hasResp        bool
	start, end     time.Time
	outcome        string
	threshold      float64
	// thresholdSource is flags.ActThresholdSourceGlobal or the backend name whose
	// own threshold flag applied (see flags.DecisionBackend.ActThreshold).
	thresholdSource string
	// routed is true when the confidence router ran (outcome act or held): only
	// then is the gen_ai.evaluation.result event recorded.
	routed          bool
	confidence      float64
	err             error
	invalidReason   string
	discardedReason string
	// stateAt is the fire-time GameContext.UpdatedAt: when the state the request
	// was built on last changed (#2123, decision.max_state_age_ms).
	stateAt time.Time
	// permissionKnown is true when the decision the answers describe went through
	// the permission chain; blocked / blockReason are its result (#1295).
	permissionKnown bool
	blocked         bool
	blockReason     BlockReason
}

// notePermission records what the permission chain did with the first decision
// run since state held `before` outcomes (the decision the answers describe).
// Nothing is recorded when no decision ran.
func (rec *decideRecord) notePermission(state *LayerState, before int) {
	if len(state.Outcomes) <= before {
		return
	}
	first := state.Outcomes[before]
	rec.permissionKnown = true
	rec.blocked = !first.Dispatched
	rec.blockReason = first.BlockReason
}

// buildDecideRequest builds the FFA decide request for c with the shield-charge
// ledger view at this moment (#1870). withContext attaches the raw snapshot (for
// the in-process rules adapter only); remote requests never carry it.
func (l *Loop) buildDecideRequest(c gamecontext.GameContext, allowed []string, withContext bool) decide.Request {
	return decideRequestWith(c, allowed, withContext, l.ChargedSerials(), l.ShieldInFlight())
}

// decideRequestWith builds the FFA decide request for c whose state marks the
// charged players shield_charged and, in a hold game, adds the grants in flight to
// the players' energy (#2096). The rules path passes the snapshot its ranking used,
// so the state and the charge policy agree.
func decideRequestWith(c gamecontext.GameContext, allowed []string, withContext bool, charged map[string]bool, inFlight map[string]float64) decide.Request {
	st := decide.BuildStateWith(c, decide.Annotations{Charged: charged, ShieldInFlight: inFlight})
	req := decide.Request{State: st, Questions: decide.FFAQuestions(st, allowed)}
	if withContext {
		cc := c
		req.Context = &cc
	}
	return req
}

// requestedBackend is the normalized decision.backend value ("" counts as rules).
func requestedBackend(snapshot flags.Snapshot) string {
	if snapshot.Decision.IsRules() {
		return flags.DecisionBackendRules
	}
	return snapshot.Decision.Backend
}

// rulesDecide runs the rules engine through the decide hook: the decisions are
// exactly Evaluate's, and the agent.decide record (nil on a throttled or
// span-less evaluation) carries the answers from the same ranking. requested and
// fallbackReason describe why rules decided (requested=rules, reason "" on the
// default path).
func (l *Loop) rulesDecide(ctx context.Context, snapshot flags.Snapshot, rules RulesEngine, c gamecontext.GameContext, requested, fallbackReason string) ([]Decision, *decideRecord) {
	eng, ok := rules.(decideEngine)
	if !ok {
		return rules.Evaluate(ctx, c), nil
	}
	now := l.nowOrDefault()
	start := now()
	out, req, resp, ranked := eng.EvaluateDecide(ctx, c, func(charged map[string]bool) decide.Request {
		return decideRequestWith(c, snapshot.InterventionsAllowed, true, charged, l.ShieldInFlight())
	})
	if !ranked {
		return out, nil
	}
	rec := &decideRecord{
		requested:      requested,
		used:           RulesBackendName,
		fallbackReason: fallbackReason,
		req:            req,
		resp:           resp,
		hasResp:        true,
		start:          start,
		end:            now(),
		outcome:        DecideOutcomeAct,
		threshold:      snapshot.Decision.ActConfidenceThreshold,
	}
	iv, hasIV := resp.Find(decide.QuestionFFAIntervention)
	if hasIV {
		rec.confidence = iv.Confidence
	}
	switch {
	case fallbackReason != "":
		rec.outcome = DecideOutcomeFallback
	case len(out) == 0 || (hasIV && iv.Choice == decide.OptionNoop):
		// #1295, defensive: a noop choice never reads act. On the default rules
		// path this record is never emitted (OnEvaluate returns before any span on
		// a zero-decision cycle); it guards any future caller that emits it.
		rec.outcome = DecideOutcomeNoop
	}
	return out, rec
}

// decideRemote is decide() for decision.backend != rules. It never blocks: it
// either fires the remote call asynchronously and returns no decision (the async
// path owns this context), returns nothing because a call is in flight or the
// per-game cadence (decision.min_interval_ms from the previous call's start,
// decision.min_gap_after_answer_ms from its end) has not elapsed (the remote backend
// owns the decision; rules do not also act) or the allow-list is empty (nothing
// could act, #1298), or falls back to rules synchronously
// when the backend cannot be called at all (unknown/unavailable backend, async not
// wired, game kind not eligible, global budget exhausted).
func (l *Loop) decideRemote(ctx context.Context, snapshot flags.Snapshot, rules RulesEngine, c gamecontext.GameContext, trig EvalTrigger) ([]Decision, *decideRecord) {
	// Claim the per-game inflight slot FIRST: a cycle that loses the claim (a call
	// is in flight) builds no backend and charges neither cadence nor budget.
	if !l.inflight.CompareAndSwap(false, true) {
		return nil, nil
	}
	fired := false
	defer func() {
		if !fired {
			l.inflight.Store(false)
		}
	}()

	requested := requestedBackend(snapshot)
	backend, reason := l.decideBackends.Resolve(snapshot.Decision)
	misconfigured := reason
	if backend != nil && !l.asyncEnabled() {
		misconfigured = DecideFallbackNotWired
	}
	l.llmGate.Lock()
	l.noteMisconfigured(c, requested, misconfigured)
	l.llmGate.Unlock()
	if backend == nil {
		// Counted every cycle, like the eligibility and budget gates, so a
		// misconfigured backend (e.g. JEV_API_KEY missing) shows up in metrics
		// even while the rules engine is quiet.
		l.recordGated(ctx, reason)
		return l.rulesDecide(ctx, snapshot, rules, c, requested, reason)
	}
	if !l.asyncEnabled() {
		l.recordGated(ctx, DecideFallbackNotWired)
		return l.rulesDecide(ctx, snapshot, rules, c, requested, DecideFallbackNotWired)
	}
	switch gate := l.gateRemoteDecide(ctx, snapshot, c); gate {
	case "":
	case DecideGateInterval:
		// Not a failure: the remote backend decides at its cadence and rules stay
		// quiet in between. No span per blocked cycle; the gated counter and a
		// debug log say why.
		l.Log.Debug("agent.decide.cadence_wait", "session_id", c.SessionID, "backend", requested,
			"min_interval", snapshot.Decision.MinInterval,
			"min_gap_after_answer", snapshot.Decision.MinGapAfterAnswer)
		return nil, nil
	case DecideGateNoInterventions:
		// Nothing is allowed, so neither the backend nor rules could act: no call,
		// no rules run, not a fallback (#1298). The gated counter says why.
		return nil, nil
	default:
		return l.rulesDecide(ctx, snapshot, rules, c, requested, gate)
	}
	fired = true
	l.launchDecideAsync(ctx, snapshot, backend, requested, c, trig)
	return nil, nil
}

// isConfigFallback reports whether reason is a configuration fallback: the
// requested backend can never decide until its configuration changes
// (unknown_backend, backend_unavailable, async_not_wired). llm_not_eligible and
// llm_budget_exhausted are normal operation (shadow games, spend cap) and stay
// counter-only on a quiet cycle.
func isConfigFallback(reason string) bool {
	switch reason {
	case DecideFallbackUnknownBackend, DecideFallbackUnavailable, DecideFallbackNotWired:
		return true
	}
	return false
}

// noteMisconfigured logs when this game enters or leaves a configuration
// fallback (reason "" = none), not once per cycle: a WARN on entering or
// changing reason, an INFO when it clears. The caller holds llmGate.
func (l *Loop) noteMisconfigured(c gamecontext.GameContext, requested, reason string) {
	prev := l.misconfiguredReason
	if prev == reason {
		return
	}
	l.misconfiguredReason = reason
	if reason == "" {
		l.Log.Info("agent.decide.backend_misconfigured", "session_id", c.SessionID,
			"backend", requested, "state", "cleared", "previous_reason", prev)
		return
	}
	l.Log.Warn("agent.decide.backend_misconfigured", "session_id", c.SessionID,
		"backend", requested, "state", "entered", "reason", reason)
}

// emitQuietFallback records a configuration fallback (isConfigFallback) on which
// the rules engine emitted nothing (UC-040 A3, decide contract §7): an
// agent.decide span with outcome=fallback and the fallback reason, under its own
// agent.signal_received root like a decided cycle's. A decided cycle already
// carries that span; without this a quiet cycle left no trace, so for example a
// missing API key was invisible until a rule fired. Throttled per reason to one
// per throttle window (decision.throttle_seconds) because it can fire on every
// 10-60 Hz cycle; the agent_llm_gated_total counter still counts every cycle.
func (l *Loop) emitQuietFallback(ctx context.Context, rec *decideRecord, c gamecontext.GameContext, trig EvalTrigger) {
	if !l.throttleAllows(l.quietFallbackClock(rec.fallbackReason)) {
		return
	}
	rootCtx, root := l.startSignalReceivedSpan(ctx, c, trig)
	l.emitDecideSpan(rootCtx, rec, c)
	root.End()
}

// quietFallbackClock returns the per-reason throttle timestamp for
// emitQuietFallback, allocating it on first use. The pointer stays valid; the
// timestamp itself is read and written under mu by throttleAllows.
func (l *Loop) quietFallbackClock(reason string) *time.Time {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.lastQuietFallback == nil {
		l.lastQuietFallback = map[string]*time.Time{}
	}
	t, ok := l.lastQuietFallback[reason]
	if !ok {
		t = new(time.Time)
		l.lastQuietFallback[reason] = t
	}
	return t
}

// DecideGateInterval is the agent_llm_gated_total reason (and debug log) for a
// remote decide cycle held back by the per-game cadence: decision.min_interval_ms
// (from the previous call's admission) or decision.min_gap_after_answer_ms (from
// the previous call's end, #2123).
const DecideGateInterval = "decide_interval"

// DecideGateNoInterventions is the agent_llm_gated_total reason for a remote
// decide cycle skipped because the allow-list is empty (#1298): the only
// ffa.intervention option would be noop, so a paid call could not change anything.
const DecideGateNoInterventions = "no_interventions_allowed"

// hasAllowedInterventions reports whether allowed offers at least one
// intervention besides noop, filtered exactly like the ffa.intervention options
// (decide.FFAQuestions: empty ids and noop dropped). It is the single seam for
// "is there anything a remote decide call could choose" (later also: every
// allowed intervention currently unusable).
func hasAllowedInterventions(allowed []string) bool {
	for _, a := range allowed {
		if a != "" && a != decide.OptionNoop {
			return true
		}
	}
	return false
}

// noteNoInterventions records the empty-allow-list state for this game and logs
// at debug level only when it changes, not once per 10-60 Hz cycle. The caller
// holds llmGate.
func (l *Loop) noteNoInterventions(c gamecontext.GameContext, skipping bool) {
	if l.skippingNoInterventions == skipping {
		return
	}
	l.skippingNoInterventions = skipping
	l.Log.Debug("agent.decide.no_interventions_allowed", "session_id", c.SessionID, "skipping", skipping)
}

// gateRemoteDecide is the call gate for remote decide calls: eligibility
// (llm.eligible_game_kinds, so shadow games stay rules-only), a non-empty
// allow-list (hasAllowedInterventions, #1298), the per-game cadence
// (decision.min_interval_ms from the previous admission and, #2123,
// decision.min_gap_after_answer_ms from the previous call's end), and the GLOBAL
// per-minute budget
// llm.max_requests_per_minute shared with the post-game retro (the spend bound).
// Cadence and budget are charged only on admission, and only after the caller
// won the inflight claim. It returns "" when admitted.
func (l *Loop) gateRemoteDecide(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext) string {
	if !snapshot.LLMGate.EligibleFor(c.GameKind) {
		l.recordGated(ctx, FallbackNotEligible)
		return FallbackNotEligible
	}
	l.llmGate.Lock()
	defer l.llmGate.Unlock()
	// Before cadence and budget: an empty allow-list charges neither
	// (UC-040 BR-005: no paid call without a possible action).
	if !hasAllowedInterventions(snapshot.InterventionsAllowed) {
		l.noteNoInterventions(c, true)
		l.recordGated(ctx, DecideGateNoInterventions)
		return DecideGateNoInterventions
	}
	l.noteNoInterventions(c, false)
	t := l.nowOrDefault()()
	if iv := snapshot.Decision.MinInterval; iv > 0 && !l.lastDecideAttempt.IsZero() && t.Sub(l.lastDecideAttempt) < iv {
		l.recordGated(ctx, DecideGateInterval)
		return DecideGateInterval
	}
	// The gap runs from the END of the previous call (#2123): a timeout or a
	// failure ends a call like an answer does. lastDecideEnded is written before
	// the in-flight slot is released, and this cycle already won that slot, so
	// it always sees the end of the call before it.
	if gap := snapshot.Decision.MinGapAfterAnswer; gap > 0 && !l.lastDecideEnded.IsZero() && t.Sub(l.lastDecideEnded) < gap {
		l.recordGated(ctx, DecideGateInterval)
		return DecideGateInterval
	}
	if l.budget == nil || !l.budget.allow(t, l.budgetCap(snapshot.LLMGate.MaxRequestsPerMinute)) {
		l.recordGated(ctx, FallbackBudgetExhausted)
		return FallbackBudgetExhausted
	}
	l.lastDecideAttempt = t
	return ""
}

// launchDecideAsync launches one remote decide call off the loop (#1226), reusing
// the #917 async machinery: the per-game inflight slot (already claimed by the
// caller, released when the goroutine ends), inferWG (AwaitInflight joins it),
// rootCtx (shutdown cancels it) and the fire-cycle anchor span the async root
// Links to.
func (l *Loop) launchDecideAsync(ctx context.Context, snapshot flags.Snapshot, backend decide.Backend, requested string, c gamecontext.GameContext, trig EvalTrigger) {
	_, anchor := l.startSignalReceivedSpan(ctx, c, trig)
	fireSC := anchor.SpanContext()
	anchor.End()

	root := l.rootCtx
	if root == nil {
		root = context.Background()
	}
	// The remote call runs off the evaluation path; nothing waits for it
	// (UC-040 BR-001: the game never waits).
	l.inferWG.Add(1)
	go func() {
		defer l.inferWG.Done()
		defer l.inflight.Store(false)
		// runDecideRemote starts the gap clock (noteDecideEnded) when the answer
		// lands, while this goroutine still holds the slot (#2123).
		l.runDecideRemote(root, snapshot, backend, requested, c, fireSC)
	}()
}

// noteDecideEnded starts the decision.min_gap_after_answer_ms clock (#2123) at
// t, the moment the remote call of this game returned, whatever its outcome
// (answer, timeout, failure; a later discard or shutdown does not move it). The
// caller still holds the in-flight slot, so the next cycle that wins the slot
// always sees it.
func (l *Loop) noteDecideEnded(t time.Time) {
	l.llmGate.Lock()
	l.lastDecideEnded = t
	l.llmGate.Unlock()
}

// Discard reasons specific to the remote decide path (decide.discarded_reason).
const (
	// decideDiscardShutdown: the agent shut down while the call was in flight.
	decideDiscardShutdown = "shutdown"
	// DecideDiscardKillSwitch: the enabled kill switch was off when the answer
	// landed. Nothing acts, not even the rules fallback.
	DecideDiscardKillSwitch = "kill_switch"
	// DecideDiscardBackendChanged: decision.backend changed while the call was in
	// flight; the answer of the old backend is dropped.
	DecideDiscardBackendChanged = "backend_changed"
)

// runDecideRemote is the fired goroutine: call the backend under its timeout,
// validate, re-read the LIVE flags, then route the answer (act / held / noop /
// invalid) or fall back to rules for the CURRENT context. Everything is recorded
// on one agent.decide span.
//
// Every safety gate is evaluated against the flags as they are when the answer
// LANDS, not when the call fired: the enabled kill switch, decision.backend, the
// act threshold, interventions_allowed and the policy gates all come from a fresh
// Flags.Evaluate, so flipping a flag mid-flight takes effect on the in-flight
// answer too.
func (l *Loop) runDecideRemote(root context.Context, fired flags.Snapshot, backend decide.Backend, requested string, c gamecontext.GameContext, fireSC trace.SpanContext) {
	now := l.nowOrDefault()
	start := now()
	cfg, _ := fired.Decision.Remote(requested)
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = flags.DefaultDecisionTimeoutMs * time.Millisecond
	}

	parent := root
	if p, ok := inGameRemoteParent(root, c.GameTraceID, c.GameTraceSpanID, c.GameplayPhaseSpanID, c.GameCycleSpanID); ok {
		parent = p
	}
	opts := []trace.SpanStartOption{
		trace.WithTimestamp(start),
		trace.WithSpanKind(trace.SpanKindInternal),
	}
	opts = append(opts, fireCycleLink(fireSC)...)
	spanCtx, span := l.Tracer.Start(parent, SpanDecide, opts...)

	rec := &decideRecord{
		requested: requested,
		used:      backend.Name(),
		model:     cfg.Model,
		req:       l.buildDecideRequest(c, fired.InterventionsAllowed, false),
		start:     start,
		stateAt:   c.UpdatedAt,
	}
	// Fire-time threshold: only a placeholder for spans that end before landing
	// (none are routed then); the landing re-read below overwrites it with the
	// LIVE effective threshold, which is what routing uses.
	rec.threshold, rec.thresholdSource = fired.Decision.ActThreshold(rec.used)
	defer func() {
		l.endDecideSpan(span, rec, c)
		if rec.discardedReason != "" {
			l.decideDiscarded.Add(root, 1, otelmetric.WithAttributes(attribute.String("reason", rec.discardedReason)))
		}
	}()

	callCtx, cancel := context.WithTimeout(spanCtx, timeout)
	resp, err := backend.Decide(callCtx, rec.req)
	timedOut := err != nil && (errors.Is(err, context.DeadlineExceeded) || callCtx.Err() == context.DeadlineExceeded)
	cancel()
	rec.end = now()
	l.noteDecideEnded(rec.end)

	// Shutdown first: a call cancelled by the agent's root context is neither a
	// backend error nor a fallback. Record it, act on nothing.
	if root.Err() != nil {
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, decideDiscardShutdown
		return
	}

	switch {
	case timedOut:
		rec.fallbackReason, rec.err = DecideFallbackTimeout, err
	case errors.Is(err, decide.ErrInvalid):
		// The remote backends validate their own output (llmio.Parse runs
		// decide.Validate) and return an error wrapping decide.ErrInvalid for a
		// malformed or invalid answer: that is an invalid answer, not a
		// transport error (#1230).
		rec.fallbackReason, rec.err = DecideFallbackInvalidAnswer, err
	case err != nil:
		rec.fallbackReason, rec.err = DecideFallbackError, err
	default:
		if verr := decide.Validate(rec.req, resp); verr != nil {
			rec.fallbackReason, rec.err = DecideFallbackInvalidAnswer, verr
		} else {
			rec.resp, rec.hasResp = resp, true
			// Record the answer's confidence even when the live re-read below
			// discards it (kill switch, backend change), so the span reports
			// what the backend answered rather than 0.
			if iv, found := resp.Find(decide.QuestionFFAIntervention); found {
				rec.confidence = iv.Confidence
			}
		}
	}
	if rec.fallbackReason != "" {
		// error.type only, never the error text: a backend error can echo a
		// vendor body (and a masked key in it), and logs are exported too.
		l.Log.Warn("agent.decide.fallback",
			"session_id", l.gameID,
			"backend", requested,
			"reason", rec.fallbackReason,
			"digest", rec.req.State.Digest,
			"error.type", decideErrorType(rec))
	}

	// Re-read the LIVE flags: the safety gates apply as they are now.
	live := l.Flags.Evaluate(root)
	// The effective threshold for the answering backend, from the LIVE flags: a
	// flip of decision.jev.act_confidence_threshold (or the global) mid-flight
	// applies to the in-flight answer.
	rec.threshold, rec.thresholdSource = live.Decision.ActThreshold(rec.used)
	if !live.Enabled {
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DecideDiscardKillSwitch
		return
	}
	if requestedBackend(live) != requested {
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DecideDiscardBackendChanged
		return
	}

	// Re-read the CURRENT context: the call took up to the timeout, so the game may
	// have ended or the target may be gone (#917 re-validation).
	current, ok := l.ctxProvider.Current(l.gameID)
	state := newLayerState(live)
	// The fitness baseline of the landing (UC-040 step 14, #1699): the synchronous
	// path lifts it from the rules engine's cycle evaluation; here it is evaluated
	// on the current context with the live thresholds, the same functions and
	// thresholds runDecision hands the effect sampler, so a dispatched remote
	// decision (and its rules fallback) gets an effect sample and an id.
	if ok {
		if vals := EvaluateFitness(current, fitnessThresholds(live.Fitness)).Evaluated(); len(vals) > 0 {
			state.FitnessEvaluated = vals
		}
	}
	if rec.fallbackReason != "" {
		l.decideRemoteFallback(spanCtx, live, l.rulesFor(live), current, ok, rec, &state)
		return
	}
	l.decideRemoteApply(spanCtx, live, current, ok, rec, &state)
}

// decideRemoteFallback runs the given rules engine (rulesFor of the live flags,
// so the probe flag applies here too) for the current context after the
// remote backend could not decide, recording rules as the backend that answered.
func (l *Loop) decideRemoteFallback(ctx context.Context, snapshot flags.Snapshot, rules RulesEngine, current gamecontext.GameContext, ok bool, rec *decideRecord, state *LayerState) {
	rec.used = RulesBackendName
	rec.outcome = DecideOutcomeFallback
	rec.hasResp = false
	rec.resp = decide.Response{}
	state.DecideBackend = RulesBackendName
	if !ok {
		rec.discardedReason = DiscardStaleContext
		return
	}
	if !gameActive(current) {
		rec.discardedReason = DiscardGameEnded
		return
	}
	var decisions []Decision
	if eng, isDecide := rules.(decideEngine); isDecide {
		out, req, resp, ranked := eng.EvaluateDecide(ctx, current, func(charged map[string]bool) decide.Request {
			return decideRequestWith(current, snapshot.InterventionsAllowed, true, charged, l.ShieldInFlight())
		})
		decisions = out
		if ranked {
			rec.req, rec.resp, rec.hasResp = req, resp, true
			if iv, found := resp.Find(decide.QuestionFFAIntervention); found {
				rec.confidence = iv.Confidence
			}
		}
	} else {
		decisions = rules.Evaluate(ctx, current)
	}
	now := l.nowOrDefault()
	before := len(state.Outcomes)
	for _, d := range decisions {
		l.runDecision(ctx, snapshot, current, d, now(), state)
	}
	rec.notePermission(state, before)
}

// decideRemoteApply maps a validated remote answer to a Decision and routes it by
// confidence: at or above the effective act threshold (rec.threshold: the
// backend's own decision.<backend>.act_confidence_threshold, else the global
// decision.act_confidence_threshold) it goes through the
// normal permission chain (runDecision: allow-list, battery, rate limit) and the
// action sink; below it is held.
func (l *Loop) decideRemoteApply(ctx context.Context, snapshot flags.Snapshot, current gamecontext.GameContext, ok bool, rec *decideRecord, state *LayerState) {
	state.DecideBackend = rec.used
	d, outcome, invalidReason := mapAnswers(rec.resp)
	if iv, found := rec.resp.Find(decide.QuestionFFAIntervention); found {
		rec.confidence = iv.Confidence
	}
	switch outcome {
	case DecideOutcomeInvalid:
		rec.outcome, rec.invalidReason = DecideOutcomeInvalid, invalidReason
		return
	case DecideOutcomeNoop:
		rec.outcome = DecideOutcomeNoop
		return
	}
	rec.routed = true
	if rec.confidence < rec.threshold {
		rec.outcome = DecideOutcomeHeld
		return
	}
	rec.outcome = DecideOutcomeAct
	switch {
	case !ok:
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DiscardStaleContext
		return
	case !gameActive(current):
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DiscardGameEnded
		return
	case d.TargetSerial != "" && !targetPresent(current, d.TargetSerial):
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DiscardTargetGone
		return
	case stateTooOld(rec.stateAt, rec.end, snapshot.Decision.MaxStateAge):
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, DiscardStateTooOld
		return
	}
	before := len(state.Outcomes)
	l.runDecision(ctx, snapshot, current, d, l.nowOrDefault()(), state)
	rec.notePermission(state, before)
	if len(state.Outcomes) > before && state.Outcomes[len(state.Outcomes)-1].Dispatched {
		// A dispatched remote difficulty intervention invalidates the variance
		// baseline exactly like a rules one: arm the rules engine's cooldown.
		if r, isRecorder := l.Rules.(actedRecorder); isRecorder {
			r.RecordActed(d)
		}
	}
}

// stateTooOld reports whether the state a remote request was built on (stateAt,
// the fire-time GameContext.UpdatedAt) is older than maxAge when the answer
// landed (#2123, decision.max_state_age_ms, read live). A zero maxAge disables
// the check; a zero stateAt (never observed) never discards, like targetPresent's
// lenient treatment of an unknown signal.
func stateTooOld(stateAt, landed time.Time, maxAge time.Duration) bool {
	return maxAge > 0 && !stateAt.IsZero() && landed.Sub(stateAt) > maxAge
}

// playerTargetedInterventions are the interventions the action sink scopes to one
// player (services/agent/actions/writer.go uses TargetSerial for exactly these).
// Every other intervention is session-scoped and takes target "none".
var playerTargetedInterventions = map[string]bool{
	InterventionSendControllerEffect:    true,
	InterventionEliminatePlayer:         true,
	InterventionRevivePlayer:            true,
	InterventionAdjustPlayerSensitivity: true,
	InterventionSetPlayerHandicap:       true,
	InterventionGrantShield:             true,
	InterventionToughnessBoost:          true,
	InterventionSoftPenalty:             true,
}

// Cross-question inconsistencies (decide.invalid_reason).
const (
	invalidNoopWithTarget          = "noop_with_target"
	invalidPlayerTargetMissing     = "player_intervention_without_target"
	invalidSessionTargetedAtPlayer = "session_intervention_with_target"
	invalidMissingIntervention     = "missing_intervention_answer"
)

// mapAnswers turns a VALIDATED FFA response into a Decision: ffa.intervention is
// the intervention, ffa.target the TargetSerial ("none" -> session-scoped), and
// ffa.magnitude resolves through decide.ResolveMagnitude into Decision.Value (left
// empty for interventions without numeric bounds, so the writer default applies).
// Reason is the truncated rationale. It returns outcome noop for a consistent noop,
// invalid (with the reason) for cross-question inconsistencies, and act otherwise —
// confidence routing is the caller's job.
func mapAnswers(resp decide.Response) (Decision, string, string) {
	iv, ok := resp.Find(decide.QuestionFFAIntervention)
	if !ok {
		return Decision{}, DecideOutcomeInvalid, invalidMissingIntervention
	}
	target := decide.TargetNone
	if t, ok := resp.Find(decide.QuestionFFATarget); ok {
		target = t.Choice
	}
	if iv.Choice == decide.OptionNoop {
		if target != decide.TargetNone {
			return Decision{}, DecideOutcomeInvalid, invalidNoopWithTarget
		}
		return Decision{}, DecideOutcomeNoop, ""
	}
	playerScoped := playerTargetedInterventions[iv.Choice]
	switch {
	case playerScoped && target == decide.TargetNone:
		return Decision{}, DecideOutcomeInvalid, invalidPlayerTargetMissing
	case !playerScoped && target != decide.TargetNone:
		return Decision{}, DecideOutcomeInvalid, invalidSessionTargetedAtPlayer
	}
	d := Decision{Intervention: iv.Choice, Reason: truncateRationale(resp.Rationale)}
	if playerScoped {
		d.TargetSerial = target
	}
	if d.Reason == "" {
		d.Reason = "decided by " + resp.Backend
	}
	if m, ok := resp.Find(decide.QuestionFFAMagnitude); ok {
		if v, ok := decide.ResolveMagnitude(iv.Choice, m.Score); ok {
			d.Value = strconv.FormatFloat(v, 'g', -1, 64)
		}
	}
	return d, DecideOutcomeAct, ""
}

// truncateRationale makes untrusted model text safe for a span attribute and a
// Decision.Reason: one line, at most maxRationaleRunes runes.
func truncateRationale(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	if utf8.RuneCountInString(s) <= maxRationaleRunes {
		return s
	}
	r := []rune(s)
	return string(r[:maxRationaleRunes]) + "..."
}

// emitDecideSpan emits a recorded rules-path agent.decide span under parent (the
// cycle's agent.signal_received), backdated to the ranking it reports.
func (l *Loop) emitDecideSpan(parent context.Context, rec *decideRecord, c gamecontext.GameContext) {
	_, span := l.Tracer.Start(parent, SpanDecide,
		trace.WithTimestamp(rec.start),
		trace.WithSpanKind(trace.SpanKindInternal))
	l.setDecideAttributes(span, rec, c)
	span.End(trace.WithTimestamp(rec.end))
}

// endDecideSpan finishes the remote-path agent.decide span.
func (l *Loop) endDecideSpan(span trace.Span, rec *decideRecord, c gamecontext.GameContext) {
	if rec.end.IsZero() {
		rec.end = l.nowOrDefault()()
	}
	l.setDecideAttributes(span, rec, c)
	span.End()
}

// setDecideAttributes stamps the agent.decide schema onto span.
func (l *Loop) setDecideAttributes(span trace.Span, rec *decideRecord, c gamecontext.GameContext) {
	attrs := []attribute.KeyValue{
		semconv.GenAIAgentName(AgentName),
		// The game is the "conversation" the agent's decisions belong to.
		semconv.GenAIConversationID(c.SessionID),
		attribute.String(AttrGameID, c.SessionID),
		attribute.String(AttrSessionID, c.SessionID),
		attribute.String(AttrGameKind, c.GameKind),
		attribute.String(AttrDecideBackend, rec.used),
		attribute.String(AttrDecideBackendRequested, rec.requested),
		attribute.String(AttrDecideFallbackReason, rec.fallbackReason),
		attribute.String(AttrDecideStateDigest, rec.req.State.Digest),
		attribute.String(AttrDecideQuestions, questionsJSON(rec.req.Questions)),
		attribute.Int64(AttrDecideLatencyMs, rec.end.Sub(rec.start).Milliseconds()),
		attribute.String(AttrDecideOutcome, rec.outcome),
	}
	if rec.routed {
		attrs = append(attrs,
			attribute.Float64(AttrDecideActThreshold, rec.threshold),
			attribute.String(AttrDecideActThresholdSource, rec.thresholdSource))
	}
	if rec.model != "" {
		attrs = append(attrs, semconv.GenAIRequestModel(rec.model))
	}
	if rec.hasResp {
		attrs = append(attrs,
			attribute.String(AttrDecideAnswers, capJSON(rec.resp.Answers)),
			attribute.Float64(AttrDecideConfidence, rec.confidence),
			attribute.Float64(AttrDecideConfidenceMin, minConfidence(rec.resp.Answers)),
			attribute.String(AttrDecideRationale, truncateRationale(rec.resp.Rationale)),
		)
	}
	if rec.invalidReason != "" {
		attrs = append(attrs, attribute.String(AttrDecideInvalidReason, rec.invalidReason))
	}
	if rec.discardedReason != "" {
		attrs = append(attrs, attribute.String(AttrDecideDiscardedReason, rec.discardedReason))
	}
	if rec.permissionKnown {
		attrs = append(attrs, attribute.Bool(AttrDecideBlocked, rec.blocked))
		if rec.blocked {
			attrs = append(attrs, attribute.String(AttrDecideBlockReason, string(rec.blockReason)))
		}
	}
	if rec.err != nil {
		attrs = append(attrs, semconv.ErrorTypeKey.String(decideErrorType(rec)))
		span.SetStatus(codes.Error, rec.fallbackReason)
	}
	span.SetAttributes(attrs...)
	if rec.routed {
		label, cmp := evaluationLabelFail, "<"
		if rec.confidence >= rec.threshold {
			label, cmp = evaluationLabelPass, ">="
		}
		span.AddEvent(evaluationEventName, trace.WithAttributes(
			semconv.GenAIEvaluationName(evaluationActThreshold),
			semconv.GenAIEvaluationScoreValue(rec.confidence),
			semconv.GenAIEvaluationScoreLabel(label),
			semconv.GenAIEvaluationExplanation("confidence "+fmtFloat(rec.confidence)+" "+cmp+
				" threshold "+fmtFloat(rec.threshold)+" ("+thresholdFlag(rec.thresholdSource)+")"),
		))
	}
}

// decideErrorType is the low-cardinality error.type: the fallback reason for a
// timeout or an invalid answer, the Go error type otherwise.
func decideErrorType(rec *decideRecord) string {
	switch rec.fallbackReason {
	case DecideFallbackTimeout, DecideFallbackInvalidAnswer:
		return rec.fallbackReason
	}
	return semconv.ErrorType(rec.err).Value.AsString()
}

// questionJSON is the compact span view of a decide.Question (no prose text).
type questionJSON struct {
	ID      string      `json:"id"`
	Kind    string      `json:"kind"`
	Options []string    `json:"options,omitempty"`
	Range   *[2]float64 `json:"range,omitempty"`
}

func questionsJSON(qs []decide.Question) string {
	out := make([]questionJSON, 0, len(qs))
	for _, q := range qs {
		v := questionJSON{ID: q.ID, Kind: string(q.Kind), Options: q.Options}
		if q.Kind == decide.KindScore {
			r := q.Range
			v.Range = &r
		}
		out = append(out, v)
	}
	return capJSON(out)
}

// capJSON marshals v and caps it at maxDecideJSONBytes (cut on a rune boundary,
// marked with truncatedJSONMarker).
func capJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	if len(b) <= maxDecideJSONBytes {
		return string(b)
	}
	cut := maxDecideJSONBytes - len(truncatedJSONMarker)
	for cut > 0 && !utf8.RuneStart(b[cut]) {
		cut--
	}
	return string(b[:cut]) + truncatedJSONMarker
}

func minConfidence(answers []decide.Answer) float64 {
	if len(answers) == 0 {
		return 0
	}
	m := answers[0].Confidence
	for _, a := range answers[1:] {
		if a.Confidence < m {
			m = a.Confidence
		}
	}
	return m
}

// thresholdFlag is the flag key an act-threshold source names, for the
// gen_ai.evaluation.result explanation.
func thresholdFlag(source string) string {
	if source == "" || source == flags.ActThresholdSourceGlobal {
		return "decision.act_confidence_threshold"
	}
	return "decision." + source + ".act_confidence_threshold"
}

func fmtFloat(v float64) string { return strconv.FormatFloat(v, 'f', 3, 64) }
