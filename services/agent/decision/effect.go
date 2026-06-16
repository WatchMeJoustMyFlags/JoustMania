package decision

import (
	"context"
	"sort"
	"sync"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

// newInterventionID returns a fresh dispatch-unique id used to tie an intervention's
// effect record to the decision that produced it (#918). A UUID v4 hex string —
// opaque, collision-resistant across agent restarts, never compared for ordering.
func newInterventionID() string { return uuid.NewString() }

// effect.go closes the game-path intervention-effect MEASUREMENT loop (#918). The
// agent attributes a decision's reasoning OUTBOUND (objectives / LayerState onto the
// decision span, #729), but until now it never measured whether the intervention
// HELPED: the infra path has fitness->rollback (#828), the game path had nothing.
//
// THE LOOP (measurement only — it never reverts an intervention):
//
//  1. At dispatch (runDecision, the applied-decision path) the loop STAMPS a baseline
//     of the game's fitness/objective signals — the SAME dotted-key quantities the
//     decision span carries as fitness.evaluated — tagged with a dispatch-unique
//     intervention id.
//  2. After a follow-up window (DefaultEffectWindow, env-tunable) the sampler reads the
//     SAME signals for the SAME game again and computes the per-objective DELTA.
//  3. It emits an effect record attributed to the intervention id: an
//     agent.intervention.effect span (per evaluated objective) AND an
//     agent_intervention_effect_delta metric, both carrying baseline/follow-up/delta +
//     window, so the M5 dashboard (#791/#792) and the #844 retro can consume it.
//
// LEAK-FREENESS / LIFECYCLE SAFETY (#923 goleak): every scheduled follow-up runs in a
// goroutine tracked on effectWG and bounded by rootCtx. The goroutine selects on the
// follow-up timer AND rootCtx.Done(); on shutdown (rootCtx cancelled) or when the
// game's partition is gone at follow-up time it emits a clearly-marked ABORTED record
// (or, on shutdown, simply returns) — it never blocks shutdown and never leaks a timer
// (the timer is always stopped). AwaitInflight joins effectWG alongside the #917
// inference goroutines.
//
// PER-GAME ISOLATION (#845): the sampler re-reads the game's CURRENT signals through
// the SAME ContextProvider (MuxContextProvider) the #917 async path uses, keyed by THIS
// loop's gameID — so an effect sample for game A reads game A's signals only.
//
// BOUNDED MEMORY: the in-flight pending-effect set is capped (maxPendingEffects). When
// the cap is reached a dispatch records no baseline (the decision still dispatches
// normally — measurement is best-effort, never on the hot decision path's critical
// route). The set is drained as samples complete and emptied when the loop is dropped.

// DefaultEffectWindow is the follow-up window: how long after an intervention is
// dispatched the loop samples the game's fitness signals again to measure the effect
// (#918). 20s sits in the issue's 15-30s band — long enough for an intervention
// (shield, sensitivity nudge, audio cue) to move the objective signals, short enough to
// still attribute the change to that intervention. Override at startup with
// AGENT_EFFECT_WINDOW_SECONDS (see SetEffectWindow / main.go).
const DefaultEffectWindow = 20 * time.Second

// maxPendingEffects bounds the in-flight follow-up samples PER LOOP (per game) so a
// burst of dispatched interventions cannot launch unbounded goroutines/timers. At the
// per-game weighted rate limit (max_interventions_per_minute) a 20s window holds only a
// handful of samples in flight; 64 is comfortably above that while still a hard ceiling.
const maxPendingEffects = 64

// effectSampler is the per-Loop intervention-effect machinery (#918), embedded in Loop
// like asyncInferState. It owns the follow-up window, the in-flight goroutine tracking,
// the bounded pending set, the effect metric, and the test seams (now / afterFunc).
type effectSampler struct {
	// window is the follow-up delay; zero means DefaultEffectWindow. Set by
	// SetEffectWindow at construction.
	window time.Duration
	// effectWG tracks every in-flight follow-up goroutine so AwaitInflight can join
	// them at shutdown (no leak) and tests can wait for a sample to land deterministically.
	effectWG sync.WaitGroup
	// pendingMu guards pending; the concurrent trace+metrics Export handlers both drive
	// runDecision and so both schedule samples.
	pendingMu sync.Mutex
	// pending counts in-flight follow-up samples for THIS loop (bounded by
	// maxPendingEffects). A scheduled sample increments it; the goroutine decrements it
	// when it completes. Never goes negative.
	pending int
	// effectDelta records agent_intervention_effect_delta. Never nil (NewLoop seeds a
	// no-op), so the record path is always safe.
	effectDelta otelmetric.Float64Histogram
	// afterFunc schedules fn to run after d, returning a stop function. It is the test
	// seam for the follow-up timer: production uses realAfterFunc (a context-cancellable
	// time.Timer); tests inject a manual scheduler to fire the window deterministically
	// with no real sleep. Nil means realAfterFunc.
	afterFunc func(d time.Duration, fn func()) (stop func() bool)
}

// SetEffectWindow overrides the follow-up window (#918). A non-positive value leaves
// DefaultEffectWindow in place. main.go reads AGENT_EFFECT_WINDOW_SECONDS and passes it
// here. Set at construction, before the loop serves; not safe to call concurrently with
// OnEvaluate.
func (l *Loop) SetEffectWindow(d time.Duration) {
	if d <= 0 {
		return
	}
	l.window = d
}

// effectWindow returns the configured follow-up window, defaulting to
// DefaultEffectWindow when unset.
func (l *Loop) effectWindow() time.Duration {
	if l.window <= 0 {
		return DefaultEffectWindow
	}
	return l.window
}

// AwaitEffects blocks until every in-flight follow-up sample for this loop has
// completed (#918), mirroring AwaitInflight for the #917 inference goroutines. Tests
// call it to make a follow-up land deterministically before asserting; shutdown joins
// it via AwaitInflight. Safe to call when nothing is pending (returns immediately).
func (l *Loop) AwaitEffects() { l.effectWG.Wait() }

// scheduleEffectSample stamps the baseline fitness signals for a just-DISPATCHED
// intervention and schedules a follow-up sample after the window (#918). It is called
// from runDecision on the applied-decision path ONLY (blocked decisions never moved the
// game, so there is nothing to measure). It returns the dispatch-unique intervention id
// so the decision span can carry it (joining decision->effect in Jaeger), or "" when no
// sample was scheduled (effect measurement not wired, no baseline signals, or the
// pending cap reached) — the caller treats "" as "no effect id to stamp".
//
// It NEVER blocks the hot decision path: the baseline is a cheap map copy and the
// follow-up runs in its own goroutine. Without a context provider (the re-validation
// source, same as the #917 async path) there is no way to read the game's future
// signals, so it no-ops — an un-wired Loop is behavior-unchanged.
func (l *Loop) scheduleEffectSample(decisionCtx context.Context, c gamecontext.GameContext, d Decision, baselineVals map[string]float64, thresholds FitnessThresholds) string {
	// No re-validation source => cannot read the future context => nothing to measure.
	if l.ctxProvider == nil {
		return ""
	}
	if len(baselineVals) == 0 {
		// The cycle evaluated no fitness signals (e.g. the engine had no signals, or a
		// Probe/Noop engine): there is no baseline to compare against, so skip — emitting
		// an all-zero delta would be misleading, not measurement.
		return ""
	}

	// Bound the in-flight set so a burst of dispatches cannot launch unbounded
	// goroutines/timers. Over the cap we skip scheduling (the dispatch itself still
	// happens; only the measurement is dropped, best-effort by design).
	l.pendingMu.Lock()
	if l.pending >= maxPendingEffects {
		l.pendingMu.Unlock()
		return ""
	}
	l.pending++
	l.pendingMu.Unlock()

	now := l.now
	if now == nil {
		now = time.Now
	}
	id := newInterventionID()
	dispatchedAt := now()
	objective := d.ObjectiveServed
	if objective == "" {
		objective = DefaultObjectives
	}

	// Defensive copy: the baseline is read by the follow-up goroutine seconds later, so
	// it must not alias a map the caller may reuse/mutate on a later cycle.
	baseline := make(map[string]float64, len(baselineVals))
	for k, v := range baselineVals {
		baseline[k] = v
	}

	root := l.rootCtx
	if root == nil {
		root = context.Background()
	}
	window := l.effectWindow()

	sample := pendingEffect{
		id:           id,
		intervention: d.Intervention,
		objective:    objective,
		baseline:     baseline,
		thresholds:   thresholds,
		dispatchedAt: dispatchedAt,
		window:       window,
		// Capture the game-session trace ids from the GameContext in scope at schedule
		// time (#1133), stamped as searchable attributes on the deferred effect span.
		// nil-safe: empty ids => no attribute.
		gameTraceID:     c.GameTraceID,
		gameTraceSpanID: c.GameTraceSpanID,
		// Capture the CAUSING agent.decision span's SpanContext while it is still live
		// (decisionCtx is the decision span's context — it calls schedule). #1178: the effect
		// span (emitted seconds later) is started as a remote CHILD of this decision span, so
		// decision->effect is structural (nested) and lands inside the game trace transitively
		// (the decision is itself under the game span). Invalid SpanContext (no-op tracer /
		// no active span) => no remote parent => the effect span stays its own root.
		decisionSC: trace.SpanContextFromContext(decisionCtx),
	}

	l.effectWG.Add(1)
	fired := make(chan struct{})

	after := l.afterFunc
	if after == nil {
		after = realAfterFunc(root)
	}
	stop := after(window, func() { close(fired) })

	go func() {
		defer l.effectWG.Done()
		defer l.releasePending()
		select {
		case <-root.Done():
			// Shutdown / loop dropped before the window elapsed. Stop the timer (no leak)
			// and emit nothing: a measurement we never took is not a result. The decision
			// itself was already audited on its own trace.
			stop()
			return
		case <-fired:
			l.emitEffectSample(root, sample)
		}
	}()
	return id
}

// releasePending decrements the in-flight pending counter, floored at zero.
func (l *Loop) releasePending() {
	l.pendingMu.Lock()
	if l.pending > 0 {
		l.pending--
	}
	l.pendingMu.Unlock()
}

// pendingEffect is one scheduled follow-up sample's captured state (#918): the baseline
// signals stamped at dispatch, the thresholds to recompute the SAME fitness functions
// against the follow-up context, and the attribution (id / intervention / objective).
type pendingEffect struct {
	id           string
	intervention string
	objective    string
	baseline     map[string]float64
	thresholds   FitnessThresholds
	dispatchedAt time.Time
	window       time.Duration
	// gameTraceID/gameTraceSpanID are the game-session trace ids captured at schedule
	// time (#1133), stamped as searchable attributes on the effect span. Empty => no attr.
	gameTraceID     string
	gameTraceSpanID string
	// decisionSC is the SpanContext of the agent.decision span that caused this effect,
	// captured live at schedule time (#1178). The effect root is started as a remote CHILD
	// of it (nested under the decision in the game trace). Invalid => effect stays own-root.
	decisionSC trace.SpanContext
}

// emitEffectSample is the follow-up half (#918): it re-reads the game's CURRENT signals
// through the ContextProvider (same per-game source the #917 async path uses), recomputes
// the SAME fitness functions with the dispatch-time thresholds, computes the per-signal
// delta, and emits the agent.intervention.effect span(s) + agent_intervention_effect_delta
// metric. If the partition is gone (game ended/evicted before the window elapsed) it emits
// a single ABORTED record — clearly marked, no delta, no metric — so the cancellation is
// visible rather than silent.
func (l *Loop) emitEffectSample(ctx context.Context, s pendingEffect) {
	current, ok := l.ctxProvider.Current(l.gameID)
	windowSeconds := s.window.Seconds()

	// One backdated root span (to dispatch time) parents the per-objective effect spans,
	// so a Jaeger trace shows the dispatch->follow-up arc, mirroring the #917 apply root.
	//
	// In-game re-parenting (#1178, refining #1174): the effect measurement is part of the
	// game (it measures what an in-game decision did), so the effect span is started as a
	// remote CHILD of its CAUSING agent.decision span instead of LINKED to the game trace.
	// The decision span is itself (transitively) under the game span — its cycle root
	// agent.signal_received was re-parented under the game span — so the effect lands INSIDE
	// the game trace, nested under the decision that caused it: the "no clue what it relates
	// to" gap is closed structurally. The separate game-trace Link is DROPPED (it is now
	// transitive); the standalone decisionLink is likewise dropped (the decision is now the
	// PARENT, so a Link to it would be a self-link). game.id stays the query axis, and the
	// game trace ids stay as searchable span attributes. Graceful fallback: an invalid
	// decisionSC (no-op tracer / no active decision span) yields no remote parent, so the
	// effect span stays its own root exactly as the nil-safe decisionLink did before.
	startOpts := []trace.SpanStartOption{
		trace.WithTimestamp(s.dispatchedAt),
		trace.WithSpanKind(trace.SpanKindInternal),
		trace.WithAttributes(
			attribute.String(AttrSessionID, l.gameID),
			attribute.String(AttrGameID, l.gameID),
			attribute.String(AttrEffectInterventionID, s.id),
			// Stamp the SAME key the decision span uses (#1174 consistency) so one tag
			// joins decision<->effect even on backends that don't surface Link attrs.
			attribute.String(AttrDecisionInterventionID, s.id),
			attribute.String(AttrEffectIntervention, s.intervention),
			attribute.String(AttrEffectObjective, s.objective),
			attribute.Float64(AttrEffectWindowSeconds, windowSeconds),
		),
	}
	if s.gameTraceID != "" {
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceID, s.gameTraceID)))
	}
	if s.gameTraceSpanID != "" {
		startOpts = append(startOpts, trace.WithAttributes(attribute.String(AttrGameTraceSpanID, s.gameTraceSpanID)))
	}
	effectParent := ctx
	if s.decisionSC.IsValid() {
		effectParent = trace.ContextWithRemoteSpanContext(ctx, s.decisionSC)
	}
	rootCtx, root := l.Tracer.Start(effectParent, SpanInterventionEffect, startOpts...)
	defer root.End()

	if !ok {
		// Game ended / partition evicted before the window elapsed (#918 cancellation):
		// there is no current context to sample, so the effect is unmeasurable. Mark it
		// aborted on the root and emit nothing further — no fabricated delta, no metric.
		root.SetAttributes(attribute.Bool(AttrEffectAborted, true))
		return
	}

	followUp := EvaluateFitness(current, s.thresholds).Evaluated()

	// Walk the baseline signal vocabulary in a stable order so the spans/metric are
	// deterministic. Each signal that is still present at follow-up gets a delta; a
	// signal that disappeared (the objective stopped evaluating) is recorded aborted.
	for _, key := range sortedKeys(s.baseline) {
		base := s.baseline[key]
		fu, present := followUp[key]
		_, span := l.Tracer.Start(rootCtx, SpanInterventionEffect,
			trace.WithSpanKind(trace.SpanKindInternal),
			trace.WithAttributes(
				// game.id + the intervention id on the per-signal child too (#1174), so a
				// child span found in isolation is still attributable to its game + cause.
				attribute.String(AttrGameID, l.gameID),
				attribute.String(AttrEffectInterventionID, s.id),
				attribute.String(AttrDecisionInterventionID, s.id),
				attribute.String(AttrEffectIntervention, s.intervention),
				attribute.String(AttrEffectObjective, s.objective),
				attribute.String(AttrEffectSignal, key),
				attribute.Float64(AttrEffectWindowSeconds, windowSeconds),
				attribute.Float64(AttrEffectBaseline, base),
			),
		)
		if !present {
			// The signal was evaluable at dispatch but not at follow-up (the objective's
			// required signals went missing). We cannot honestly compute a delta, so mark
			// this signal aborted rather than treating absence as zero.
			span.SetAttributes(attribute.Bool(AttrEffectAborted, true))
			span.End()
			continue
		}
		delta := fu - base
		span.SetAttributes(
			attribute.Float64(AttrEffectFollowUp, fu),
			attribute.Float64(AttrEffectDelta, delta),
		)
		span.End()

		l.effectDelta.Record(ctx, delta, otelmetric.WithAttributes(
			attribute.String(AttrEffectIntervention, s.intervention),
			attribute.String(AttrEffectObjective, s.objective),
			attribute.String(AttrEffectSignal, key),
		))
	}
}

// sortedKeys returns the map keys sorted, for deterministic span/metric emission order.
func sortedKeys(m map[string]float64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// realAfterFunc is the production follow-up scheduler: a time.Timer whose firing is
// suppressed (and stopped) if root is cancelled first. The returned stop function stops
// the timer (idempotent), so a shutdown-cancelled sample never leaves a timer running.
// It is the default when no afterFunc test seam is injected.
func realAfterFunc(root context.Context) func(d time.Duration, fn func()) func() bool {
	return func(d time.Duration, fn func()) func() bool {
		timer := time.NewTimer(d)
		go func() {
			select {
			case <-timer.C:
				fn()
			case <-root.Done():
				// Cancelled before firing: stop the timer and do NOT call fn. The
				// scheduling goroutine observes root.Done() on its own and returns.
				timer.Stop()
			}
		}()
		return timer.Stop
	}
}
