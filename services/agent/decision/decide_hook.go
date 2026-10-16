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
//     The legacy mode / model flags are ignored while decision.backend != rules.
//
// Every remote call (and every rules decision) is recorded on one agent.decide
// span: OTel GenAI semantic conventions where a name exists
// (gen_ai.agent.name, gen_ai.conversation.id, gen_ai.request.model, error.type,
// and a gen_ai.evaluation.result event for the confidence-threshold judgement),
// decide.* attributes for the rest.

// SpanDecide is the per-decision decide-contract span (#1226). On the remote path
// it is the root of the async trace (a remote child of the game's gameplay_phase
// span when known, Linked to the fire cycle) and wraps the backend call — the
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
	// AttrDecideActThreshold is decision.act_confidence_threshold this cycle.
	AttrDecideActThreshold = "decide.act_confidence_threshold"
	// AttrDecideLatencyMs is the backend call latency (ranking time for rules).
	AttrDecideLatencyMs = "decide.latency_ms"
	// AttrDecideOutcome is one of the DecideOutcome* values.
	AttrDecideOutcome = "decide.outcome"
	// AttrDecideRationale is the backend's one-line rationale, truncated.
	AttrDecideRationale = "decide.rationale"
	// AttrDecideInvalidReason names the cross-question inconsistency behind
	// outcome=invalid.
	AttrDecideInvalidReason = "decide.invalid_reason"
	// AttrDecideDiscardedReason names the re-validation failure behind
	// outcome=discarded (stale_context / game_ended / target_gone).
	AttrDecideDiscardedReason = "decide.discarded_reason"
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
	EvaluateDecide(ctx context.Context, c gamecontext.GameContext, build func() decide.Request) ([]Decision, decide.Request, decide.Response, bool)
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
	// routed is true when the confidence router ran (outcome act or held): only
	// then is the gen_ai.evaluation.result event recorded.
	routed          bool
	confidence      float64
	err             error
	invalidReason   string
	discardedReason string
}

// buildDecideRequest builds the FFA decide request for c. withContext attaches
// the raw snapshot (for the in-process rules adapter only); remote requests never
// carry it.
func buildDecideRequest(c gamecontext.GameContext, allowed []string, withContext bool) decide.Request {
	st := decide.BuildState(c)
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
func (l *Loop) rulesDecide(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, requested, fallbackReason string) ([]Decision, *decideRecord) {
	eng, ok := l.Rules.(decideEngine)
	if !ok {
		return l.Rules.Evaluate(ctx, c), nil
	}
	now := l.nowOrDefault()
	start := now()
	out, req, resp, ranked := eng.EvaluateDecide(ctx, c, func() decide.Request {
		return buildDecideRequest(c, snapshot.InterventionsAllowed, true)
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
	if fallbackReason != "" {
		rec.outcome = DecideOutcomeFallback
	}
	if iv, ok := resp.Find(decide.QuestionFFAIntervention); ok {
		rec.confidence = iv.Confidence
	}
	return out, rec
}

// decideRemote is decide() for decision.backend != rules. It never blocks: it
// either fires the remote call asynchronously and returns no decision (the async
// path owns this context), returns nothing because a call is in flight or the
// per-game cadence has not elapsed (the remote backend owns the decision; rules
// do not also act), or falls back to rules synchronously when the backend cannot
// be called at all (unknown/unavailable backend, not eligible, budget exhausted).
func (l *Loop) decideRemote(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, trig EvalTrigger) ([]Decision, *decideRecord) {
	// A call is already in flight for this game: it owns this context. Checked
	// first so a skipped cycle neither builds a backend nor charges the gate.
	if l.inflight.Load() {
		return nil, nil
	}
	requested := requestedBackend(snapshot)
	backend, reason := l.decideBackends.Resolve(snapshot.Decision)
	if backend == nil {
		return l.rulesDecide(ctx, snapshot, c, requested, reason)
	}
	if !l.asyncEnabled() {
		return l.rulesDecide(ctx, snapshot, c, requested, DecideFallbackNotWired)
	}
	if !l.remoteDecideAdmitted(snapshot, c) {
		return nil, nil
	}
	// The #847 call gate bounds API spend: eligibility (shadow games stay
	// rules-only), per-game cadence, global per-minute budget. Cadence is not a
	// failure (the remote backend decides at its cadence), so it yields no
	// decision; ineligible or out-of-budget cycles degrade to rules.
	if gate := l.gateLLM(ctx, snapshot, c); gate != "" {
		if gate == FallbackInterval {
			return nil, nil
		}
		return l.rulesDecide(ctx, snapshot, c, requested, gate)
	}
	l.fireDecideAsync(ctx, snapshot, backend, requested, c, trig)
	return nil, nil
}

// remoteDecideAdmitted is the seam for a remote-decision admission gate (#1231).
// It deliberately does NOT apply the #1207 length gate (shouldUpgradeToAsyncLLM):
// that gate routes 6-7s games to rules almost always, so the remote backend would
// never decide in a short FFA game. #1231 decides what (if anything) goes here.
func (l *Loop) remoteDecideAdmitted(flags.Snapshot, gamecontext.GameContext) bool {
	return true
}

// fireDecideAsync launches one remote decide call off the loop (#1226), reusing
// the #917 async machinery: the per-game inflight CAS (at most one call per game,
// shared with the legacy llm path), inferWG (AwaitInflight joins it), rootCtx
// (shutdown cancels it) and the fire-cycle anchor span the async root Links to.
// It returns false when another call won the inflight claim.
func (l *Loop) fireDecideAsync(ctx context.Context, snapshot flags.Snapshot, backend decide.Backend, requested string, c gamecontext.GameContext, trig EvalTrigger) bool {
	if !l.inflight.CompareAndSwap(false, true) {
		return false
	}
	_, anchor := l.startSignalReceivedSpan(ctx, c, trig)
	fireSC := anchor.SpanContext()
	anchor.End()

	root := l.rootCtx
	if root == nil {
		root = context.Background()
	}
	l.inferWG.Add(1)
	go func() {
		defer l.inferWG.Done()
		defer l.inflight.Store(false)
		l.runDecideRemote(root, snapshot, backend, requested, c, fireSC)
	}()
	return true
}

// runDecideRemote is the fired goroutine: call the backend under its timeout,
// validate, then route the answer (act / held / noop / invalid) or fall back to
// rules for the CURRENT context. Everything is recorded on one agent.decide span.
func (l *Loop) runDecideRemote(root context.Context, snapshot flags.Snapshot, backend decide.Backend, requested string, c gamecontext.GameContext, fireSC trace.SpanContext) {
	now := l.nowOrDefault()
	start := now()
	cfg, _ := snapshot.Decision.Remote(requested)
	timeout := cfg.Timeout
	if timeout <= 0 {
		timeout = flags.DefaultDecisionTimeoutMs * time.Millisecond
	}

	parent := root
	if p, ok := inGameRemoteParent(root, c.GameTraceID, c.GameTraceSpanID, c.GameplayPhaseSpanID); ok {
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
		req:       buildDecideRequest(c, snapshot.InterventionsAllowed, false),
		start:     start,
		threshold: snapshot.Decision.ActConfidenceThreshold,
	}
	defer func() { l.endDecideSpan(span, rec, c) }()

	callCtx, cancel := context.WithTimeout(spanCtx, timeout)
	resp, err := backend.Decide(callCtx, rec.req)
	timedOut := err != nil && (errors.Is(err, context.DeadlineExceeded) || callCtx.Err() == context.DeadlineExceeded)
	cancel()
	rec.end = now()

	switch {
	case timedOut:
		rec.fallbackReason, rec.err = DecideFallbackTimeout, err
	case err != nil:
		rec.fallbackReason, rec.err = DecideFallbackError, err
	default:
		if verr := decide.Validate(rec.req, resp); verr != nil {
			rec.fallbackReason, rec.err = DecideFallbackInvalidAnswer, verr
		} else {
			rec.resp, rec.hasResp = resp, true
		}
	}
	if rec.fallbackReason != "" {
		l.Log.Warn("agent.decide.fallback",
			"session_id", l.gameID,
			"backend", requested,
			"reason", rec.fallbackReason,
			"digest", rec.req.State.Digest,
			"error", rec.err)
	}

	if root.Err() != nil {
		// Agent shutdown: record what happened, act on nothing.
		rec.outcome, rec.discardedReason = DecideOutcomeDiscarded, decideDiscardShutdown
		return
	}

	// Re-read the CURRENT context: the call took up to the timeout, so the game may
	// have ended or the target may be gone (#917 re-validation).
	current, ok := l.ctxProvider.Current(l.gameID)
	state := newLayerState(snapshot)
	if rec.fallbackReason != "" {
		l.decideRemoteFallback(spanCtx, snapshot, current, ok, rec, &state)
		return
	}
	l.decideRemoteApply(spanCtx, snapshot, current, ok, rec, &state)
}

// decideRemoteFallback runs the rules engine for the current context after the
// remote backend could not decide, recording rules as the backend that answered.
func (l *Loop) decideRemoteFallback(ctx context.Context, snapshot flags.Snapshot, current gamecontext.GameContext, ok bool, rec *decideRecord, state *LayerState) {
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
	if eng, isDecide := l.Rules.(decideEngine); isDecide {
		out, req, resp, ranked := eng.EvaluateDecide(ctx, current, func() decide.Request {
			return buildDecideRequest(current, snapshot.InterventionsAllowed, true)
		})
		decisions = out
		if ranked {
			rec.req, rec.resp, rec.hasResp = req, resp, true
			if iv, found := resp.Find(decide.QuestionFFAIntervention); found {
				rec.confidence = iv.Confidence
			}
		}
	} else {
		decisions = l.Rules.Evaluate(ctx, current)
	}
	now := l.nowOrDefault()
	for _, d := range decisions {
		l.runDecision(ctx, snapshot, current, d, now(), state)
	}
}

// decideRemoteApply maps a validated remote answer to a Decision and routes it by
// confidence: at or above decision.act_confidence_threshold it goes through the
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
	}
	before := len(state.Outcomes)
	l.runDecision(ctx, snapshot, current, d, l.nowOrDefault()(), state)
	if len(state.Outcomes) > before && state.Outcomes[len(state.Outcomes)-1].Dispatched {
		// A dispatched remote difficulty intervention invalidates the variance
		// baseline exactly like a rules one: arm the rules engine's cooldown.
		if r, isRecorder := l.Rules.(actedRecorder); isRecorder {
			r.RecordActed(d)
		}
	}
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
	InterventionPartialShield:           true,
	InterventionSoftPenalty:             true,
}

// decideDiscardShutdown is decide.discarded_reason when the agent shut down while
// the call was in flight.
const decideDiscardShutdown = "shutdown"

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
		attribute.Float64(AttrDecideActThreshold, rec.threshold),
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
				" threshold "+fmtFloat(rec.threshold)),
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

func fmtFloat(v float64) string { return strconv.FormatFloat(v, 'f', 3, 64) }
