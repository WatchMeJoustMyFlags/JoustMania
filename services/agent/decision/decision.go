// Package decision holds the agent's decision loop: a gated, throttled hook that
// turns a GameContext snapshot into interventions via a pluggable rules engine
// and action sink, and emits the agent.span_received -> agent.decision ->
// agent.action audit trace for every evaluation that produces decisions
// (issue #724). ObjectiveRules (#726) is the objective-weighted rules engine;
// the action sink stays a no-op until the intervention API (#730).
//
// On every gated cycle the loop evaluates the agent's OpenFeature control flags
// across all four layers (issues #727, #728) — never cached at startup — and
// applies them in order:
//
//	Existence:  enabled    kill switch; false short-circuits the whole loop
//	                       (a throttled agent.disabled span still records the
//	                       evaluated flags so "agent off" is visible in traces)
//	            mode       selects the "rules" vs "llm" decision path
//	Objective:  objectives session-goal weights driven into the rules engine via
//	                       a LiveObjectives source (#726 engine reads it each cycle)
//	Capability: model, prompt_variant   recorded for the M4 LLM path
//	Permission: interventions_allowed   allow-list gate (#727)
//	            policy.battery_threshold blocks player-targeted interventions when
//	                                     the target's battery is below threshold
//	            policy.max_interventions_per_minute  weighted sliding-window rate
//	                                     limit across all dispatched interventions
//	            policy.movement_variance_window      recorded; parameterizes the
//	                                     #726/#731 variance rules
//
// Every decision that the rules return is audited as an agent.decision ->
// agent.action span pair (#724); decisions blocked by any permission gate carry
// decision.blocked=true (with the specific BlockReason) and are NOT applied,
// never silently dropped. Every evaluated flag value plus the per-decision
// outcomes are captured in a LayerState (see layerstate.go) — the span-attribute
// source of truth for #729 — which OnEvaluate returns and retains.
package decision

import (
	"context"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// Decision is a single intervention the rules engine wants applied.
type Decision struct {
	// Intervention is an identifier from interventions.allowed
	// (docs/research/722-intervention-surface.md §6), e.g. "grant_shield".
	Intervention string
	// TargetSerial scopes the intervention to one player; empty = session-scoped.
	TargetSerial string
	// Reason is a human-readable explanation, recorded as decision.reason.
	Reason string
	// ObjectiveServed names the session objective this decision serves
	// (endurance/balanced/accelerate/chaos), recorded as
	// decision.objective_served.
	ObjectiveServed string
	// Fitness holds the fitness values the rule evaluated to reach this
	// decision (e.g. session_duration, target_session_seconds), recorded as
	// fitness.evaluated.
	Fitness map[string]float64
	// Objectives holds the active objective weights used to score this
	// decision, recorded as agent.objectives. Nil/empty (the Noop/Probe/stub
	// engines) renders the "unset" placeholder.
	Objectives map[string]float64
}

// RulesEngine turns a context snapshot into zero or more Decisions. The
// context carries the active trace; an LLM-backed engine records its inference
// call as a gen_ai.* span (GenAI semantic conventions). The objective weights
// are supplied out-of-band through the engine's ObjectivesSource (the loop
// publishes the per-cycle flag value via LiveObjectives before calling here).
type RulesEngine interface {
	Evaluate(context.Context, gamecontext.GameContext) []Decision
}

// ActionSink applies one decision to the outside world. It is called once per
// decision inside that decision's agent.action span.
type ActionSink interface {
	Apply(context.Context, Decision) error
}

// FlagSource resolves the agent control flags for a single decision cycle.
// *flags.Flags satisfies it; tests supply a fake.
type FlagSource interface {
	Evaluate(ctx context.Context) flags.Snapshot
}

// objectivePublisher is the optional seam the loop uses to push the per-cycle
// objectives flag into the rules engine. *ObjectiveRules (via its LiveObjectives
// source) satisfies it; engines that ignore objectives (Noop/Probe) do not, and
// the loop simply skips publication for them.
type objectivePublisher interface {
	SetObjectives(map[string]float64)
}

// EvalTrigger describes the OTLP Export that triggered an evaluation, used to
// annotate the agent.span_received root span with rpc.* semconv attributes and
// backdate it to the moment the Export arrived.
type EvalTrigger struct {
	// Signal is the OTLP signal type: "traces" or "metrics".
	Signal string
	// RPCService is the full OTLP gRPC service name, e.g.
	// "opentelemetry.proto.collector.metrics.v1.MetricsService".
	RPCService string
	// T0 is when the Export handler started processing.
	T0 time.Time
}

// throttleInterval bounds how often the evaluate log line is emitted.
const throttleInterval = time.Second

// Loop wires the flag source, rules engine, and action sink together and is
// invoked once per gated signal update. It is safe for concurrent use: the gRPC
// trace and metrics Export handlers each invoke OnEvaluate from their own
// goroutines.
type Loop struct {
	Flags   FlagSource
	Rules   RulesEngine
	Actions ActionSink
	Log     *slog.Logger
	// Tracer produces the audit spans; tests inject a recording tracer.
	Tracer trace.Tracer

	// mu guards the log-throttle state and lastLayer below.
	mu      sync.Mutex
	lastLog time.Time
	now     func() time.Time

	// limiter is the unified weighted per-minute rate limiter spanning all
	// dispatched interventions across cycles (#726 + #728, see ratelimit.go). It
	// is mutex-guarded internally for the concurrent Export handlers.
	limiter rateLimiter
	// lastLayer is the most recent fully-evaluated LayerState, retained so #729
	// (span attribution) and tests can read the per-cycle layer snapshot.
	lastLayer LayerState
}

// NewLoop builds a Loop with the no-op rules/actions stubs, the global tracer,
// and the given flag source. flagSource may be nil, in which case a disabled
// source is used so the kill switch defaults closed. log may be nil, in which
// case slog.Default() is used.
func NewLoop(flagSource FlagSource, log *slog.Logger) *Loop {
	if log == nil {
		log = slog.Default()
	}
	if flagSource == nil {
		flagSource = disabledFlags{}
	}
	return &Loop{
		Flags:   flagSource,
		Rules:   NoopRules{},
		Actions: NoopActions{},
		Log:     log,
		Tracer:  otel.Tracer(instrumentationName),
		now:     time.Now,
	}
}

// OnEvaluate runs one evaluation pass and returns the cycle's LayerState (also
// retained on the loop for #729). Steps:
//  1. evaluate all four flag layers (never cached);
//  2. existence — if enabled is false, emit a throttled agent.disabled span with
//     the evaluated-flag attribution and return the disabled LayerState before
//     any rules run;
//  3. publish the objectives flag into the rules engine and run the rules;
//  4. when the rules return at least one decision, emit the audit trace lazily
//     (idle evaluations cost no spans); the root span is backdated to trig.T0 so
//     its duration covers the whole Export processing;
//  5. per decision: run the permission chain (allow-list -> battery threshold ->
//     weighted rate limit), record the outcome (dispatched or the BlockReason)
//     in the LayerState and on the decision/action spans, and apply only the
//     fully-permitted decisions through the action sink.
func (l *Loop) OnEvaluate(ctx context.Context, c gamecontext.GameContext, trig EvalTrigger) LayerState {
	now := l.now
	if now == nil {
		now = time.Now
	}

	snapshot := l.Flags.Evaluate(ctx)
	state := newLayerState(snapshot)

	// Existence layer: enabled=false short-circuits before any rules evaluation.
	// This is the safe default when flagd is unreachable. The disabled state is
	// still recorded AND emitted as a throttled agent.disabled span so a Jaeger
	// trace shows "agent off" with the flags in effect (#729). Throttled so a
	// disabled agent under heavy signal load does not flood the trace backend.
	if !snapshot.Enabled {
		l.emitDisabledSpan(ctx, snapshot, c, trig, state)
		l.setLastLayer(state)
		return state
	}

	// Objective layer: publish the per-cycle objectives flag into the rules
	// engine. The engine reads them inside Evaluate; engines that ignore
	// objectives (Noop/Probe) skip this.
	if pub, ok := l.Rules.(objectivePublisher); ok {
		pub.SetObjectives(snapshot.Objectives)
	}

	if l.shouldLog() {
		l.Log.Info("agent.evaluate",
			"session_id", c.SessionID,
			"player_count", len(c.Players),
			"game_mode", derefStr(c.Session.GameMode),
			"duration", derefFloat(c.Session.DurationSeconds),
			"mode", snapshot.Mode,
			"model", snapshot.Capability.Model,
			"prompt_variant", snapshot.Capability.PromptVariant,
			"battery_threshold", snapshot.Policy.BatteryThreshold,
			"variance_window", snapshot.Policy.MovementVarianceWindow,
			"max_per_minute", snapshot.Policy.MaxInterventionsPerMinute,
		)
	}

	decisions := l.decide(ctx, snapshot, c)
	if len(decisions) == 0 {
		l.setLastLayer(state) // no decision, no trace
		return state
	}

	// The root span wraps the inbound OTLP Export with rpc.* semconv attrs and
	// kind SERVER. There is no otelgrpc interceptor on this server today, so
	// this is the only span representing the Export. CAVEAT: if
	// otelgrpc.NewServerHandler is ever added to the agent's gRPC server,
	// demote this span to SpanKindInternal and drop the rpc.* attributes to
	// avoid a duplicate server span for the same RPC.
	rootCtx, root := l.Tracer.Start(ctx, SpanReceived,
		trace.WithTimestamp(trig.T0),
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			semconv.RPCSystemGRPC,
			semconv.RPCService(trig.RPCService),
			semconv.RPCMethod("Export"),
			attribute.String("otlp.signal", trig.Signal),
			attribute.String("session.id", c.SessionID),
		),
	)
	defer root.End()

	for _, d := range decisions {
		l.runDecision(rootCtx, snapshot, c, d, now(), &state)
	}

	l.setLastLayer(state)
	return state
}

// LastLayerState returns the LayerState from the most recent OnEvaluate call.
// Issue #729 reads it to populate the decision span.
func (l *Loop) LastLayerState() LayerState {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.lastLayer
}

// setLastLayer stores the cycle's LayerState under the loop mutex (OnEvaluate
// runs concurrently from the trace and metrics Export handler goroutines).
func (l *Loop) setLastLayer(state LayerState) {
	l.mu.Lock()
	l.lastLayer = state
	l.mu.Unlock()
}

// decide selects the decision path from snapshot.Mode and runs it. The "llm"
// path (M4) does not exist yet, so it logs a note (with the capability-layer
// model/prompt selection) and falls back to the rules engine.
func (l *Loop) decide(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext) []Decision {
	switch snapshot.Mode {
	case "llm":
		l.Log.Info("agent.mode_llm_fallback",
			"note", "llm decision path not implemented (M4); falling back to rules",
			"model", snapshot.Capability.Model,
			"prompt_variant", snapshot.Capability.PromptVariant,
		)
		return l.Rules.Evaluate(ctx, c)
	default:
		// "rules" and any unrecognized mode use the deterministic rules path.
		return l.Rules.Evaluate(ctx, c)
	}
}

// runDecision emits one agent.decision span (full #724 schema) and its
// agent.action child, runs the permission chain, records the outcome in state,
// and applies the decision only if it passes every gate. Blocked decisions carry
// decision.blocked=true (and the BlockReason) on both spans and in the
// LayerState — recorded, never silently dropped.
func (l *Loop) runDecision(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time, state *LayerState) {
	allowed := snapshot.InterventionsAllowed
	cost := interventionCost(d.Intervention)
	reason, blocked := l.evaluatePermission(snapshot, c, d, now, cost)

	dCtx, dSpan := l.Tracer.Start(ctx, SpanDecision,
		trace.WithAttributes(decisionAttributes(state, d, blocked, reason)...))
	defer dSpan.End()

	// The interventions.allowed evaluation as a feature_flag.* span event
	// (semconv) — same shape the openfeature OTel hooks emit in the Python
	// services. With #727 wired, the provider is the real flagd provider.
	// NOTE: the bare event name "feature_flag" is intentional — it matches the
	// openfeature contrib TracingHook used by the Python services (current
	// semconv drafts call the log event "feature_flag.evaluation"); do not
	// "fix" this without also migrating the Python side, or the traces diverge.
	dSpan.AddEvent("feature_flag", trace.WithAttributes(
		semconv.FeatureFlagKey(interventionsAllowedFlagKey),
		semconv.FeatureFlagProviderName(flagProviderName),
		semconv.FeatureFlagResultVariant(allowedSummary(allowed)),
	))

	_, aSpan := l.Tracer.Start(dCtx, SpanAction, trace.WithAttributes(
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.Bool(AttrDecisionBlocked, blocked),
	))
	defer aSpan.End()

	if blocked {
		state.recordBlocked(d, reason, cost)
		l.Log.Warn("agent.decision_blocked",
			"blocked", true,
			"reason", string(reason),
			"intervention", d.Intervention,
			"target_serial", d.TargetSerial,
			"decision_reason", d.Reason,
			"allowed", allowed)
		return
	}

	state.recordDispatched(d, cost)
	if err := l.Actions.Apply(dCtx, d); err != nil {
		aSpan.RecordError(err)
		// semconv.ErrorType derives the low-cardinality error class from the
		// Go error's dynamic type, per the error.type convention.
		aSpan.SetAttributes(semconv.ErrorType(err))
		aSpan.SetStatus(codes.Error, err.Error())
		l.Log.Error("agent.apply_failed",
			"error", err, "intervention", d.Intervention)
	}
}

// emitDisabledSpan emits the kill-switch trace for a cycle where the existence
// layer reported the agent off (enabled=false): a root agent.span_received with
// a single agent.decision child (no action child — nothing was decided) carrying
// the existence + capability + permission attribution lifted from the disabled
// LayerState. This makes "agent off, here are the flags that were in effect"
// visible in a Jaeger trace (#729). It is throttled to one per throttleInterval
// (shared with the evaluate log) so a disabled agent under heavy signal load
// does not flood the trace backend — the steady-state disabled agent is silent.
func (l *Loop) emitDisabledSpan(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, trig EvalTrigger, state LayerState) {
	if !l.shouldLog() {
		return
	}
	rootCtx, root := l.Tracer.Start(ctx, SpanReceived,
		trace.WithTimestamp(trig.T0),
		trace.WithSpanKind(trace.SpanKindServer),
		trace.WithAttributes(
			semconv.RPCSystemGRPC,
			semconv.RPCService(trig.RPCService),
			semconv.RPCMethod("Export"),
			attribute.String("otlp.signal", trig.Signal),
			attribute.String("session.id", c.SessionID),
		),
	)
	defer root.End()

	// One agent.disabled span carrying the full flag attribution; it is named
	// distinctly (not agent.decision) so kill-switch traces are trivial to find
	// in Jaeger. There is no action child because the kill switch decided
	// nothing. AttrDecisionAction is empty (no action) and AttrDecisionBlocked is
	// false (no decision was blocked by a permission gate — the loop never ran).
	_, dSpan := l.Tracer.Start(rootCtx, SpanDisabled,
		trace.WithAttributes(decisionAttributes(&state, Decision{}, false, "")...))
	dSpan.End()
}

// evaluatePermission runs the permission chain over one decision in order:
// allow-list gate (#727), battery threshold, then the weighted rate limit
// (#728). It returns the BlockReason and whether the decision is blocked. The
// rate limit is charged ONLY when the decision passes the allow-list and
// battery gates AND fits the budget — so the budget is spent on decisions the
// agent actually dispatches, the unification chosen over #726's
// charge-every-emitted-decision model now that the loop can see permissions.
func (l *Loop) evaluatePermission(snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time, cost float64) (BlockReason, bool) {
	// 1. Allow-list gate (#727): not in interventions_allowed.
	if !snapshot.Permits(d.Intervention) {
		return ReasonNotAllowed, true
	}
	// 2. Battery threshold: block player-targeted interventions when the target's
	// battery is below the threshold. Session-scoped decisions (empty
	// TargetSerial) are unaffected; missing battery is treated as unknown (does
	// not block, but is noted).
	if l.batteryBlocks(snapshot, c, d) {
		return ReasonBatteryThreshold, true
	}
	// 3. Weighted rate limit: block when the per-minute budget is exhausted. The
	// limiter charges the cost only when it admits, so blocked decisions above do
	// not draw down the budget.
	if !l.limiter.allow(now, cost, float64(snapshot.Policy.MaxInterventionsPerMinute)) {
		return ReasonRateLimit, true
	}
	return "", false
}

// batteryBlocks reports whether a player-targeted decision must be blocked
// because the target's battery is below policy.battery_threshold. A threshold of
// 0 disables the check. Session-scoped decisions and unknown battery never block;
// unknown battery is logged so the gap is visible (and recorded for #729).
func (l *Loop) batteryBlocks(snapshot flags.Snapshot, c gamecontext.GameContext, d Decision) bool {
	if d.TargetSerial == "" || snapshot.Policy.BatteryThreshold <= 0 {
		return false
	}
	player := c.Players[d.TargetSerial]
	if player == nil || player.BatteryPct == nil {
		// Unknown battery: do not block, but make the gap visible.
		l.Log.Debug("agent.battery_unknown",
			"target_serial", d.TargetSerial,
			"intervention", d.Intervention,
			"threshold", snapshot.Policy.BatteryThreshold,
		)
		return false
	}
	return *player.BatteryPct < float64(snapshot.Policy.BatteryThreshold)
}

// shouldLog rate-limits the evaluate log line to once per throttleInterval.
// Mutex-guarded: OnEvaluate runs concurrently from the trace and metrics
// Export handler goroutines.
func (l *Loop) shouldLog() bool {
	now := l.now
	if now == nil {
		now = time.Now
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	t := now()
	if t.Sub(l.lastLog) < throttleInterval {
		return false
	}
	l.lastLog = t
	return true
}

// decisionAttributes builds the complete #724 + #729 decision-span schema for
// one decision. It lifts every evaluated flag from the cycle's LayerState
// verbatim (cycle-level: existence/capability/permission/policy), the
// path-agnostic inference attribution, and the per-decision outcome (action,
// reason, objective served, fitness, blocked + reason). Every span carries every
// attribute; subsystems that do not exist yet contribute explicit placeholder
// values (see telemetry.go). The block reason (#728) is carried only when a
// decision is blocked.
func decisionAttributes(state *LayerState, d Decision, blocked bool, reason BlockReason) []attribute.KeyValue {
	objective := d.ObjectiveServed
	if objective == "" {
		objective = DefaultObjectives
	}
	attrs := []attribute.KeyValue{semconv.GenAIAgentName(AgentName)}
	// Cycle-level flag attribution: lift the whole LayerState onto the span so a
	// single trace answers "which flags were in effect" (#729). agent.objectives
	// is recorded there from the cycle's evaluated weights; the rules engine
	// stamps the same normalized weights onto each Decision, so the per-decision
	// view (d.Objectives) is identical and not re-emitted.
	attrs = append(attrs, layerStateAttributes(state)...)
	// Per-decision attribution: the chosen action and why.
	attrs = append(attrs,
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.String(AttrDecisionReason, d.Reason),
		attribute.String(AttrDecisionObjective, objective),
		attribute.Bool(AttrDecisionBlocked, blocked),
		attribute.StringSlice(AttrFitnessEvaluated, renderFitness(d.Fitness)),
	)
	if blocked {
		attrs = append(attrs, attribute.String(AttrDecisionBlockReason, string(reason)))
	}
	return attrs
}

// layerStateAttributes lifts the cycle's evaluated flag set from the LayerState
// onto a span verbatim — the heart of #729. It is path-agnostic (rules and the
// M4 llm path converge on the same LayerState) and used by both the live
// decision span and the disabled kill-switch span. Inference attribution is
// derived from the mode: configured = the capability model flag, used = the
// engine that actually ran (rules until M4), fallback_reason set only when
// mode=llm fell back.
func layerStateAttributes(state *LayerState) []attribute.KeyValue {
	mode := state.Mode
	if mode == "" {
		mode = DefaultMode
	}
	inferenceUsed, fallback := inferenceAttribution(mode)
	attrs := []attribute.KeyValue{
		// Existence layer.
		attribute.Bool(AttrEnabled, state.Enabled),
		attribute.String(AttrMode, mode),
		// Objective layer (cycle-wide weights as evaluated this cycle).
		attribute.String(AttrObjectives, summarizeObjectives(state.Objectives)),
		// Capability layer.
		attribute.String(AttrModel, state.Model),
		attribute.String(AttrPromptVariant, state.PromptVariant),
		// Inference attribution (path-agnostic).
		attribute.String(AttrInferenceConfigured, state.Model),
		attribute.String(AttrInferenceUsed, inferenceUsed),
		attribute.String(AttrInferenceFallback, fallback),
		// Permission layer.
		attribute.String(AttrInterventionsAllowed, allowedSummary(state.InterventionsAllowed)),
		attribute.Int(AttrPolicyBatteryThreshold, state.PolicyBatteryThreshold),
		attribute.Int(AttrPolicyMovementVarianceWindow, state.PolicyMovementVarianceWin),
		attribute.Int(AttrPolicyMaxPerMinute, state.PolicyMaxPerMinute),
	}
	// Fitness layer (#731 hook): only present when populated, so the attribute
	// stays absent until the fitness engine lands rather than carrying an
	// empty-slice placeholder that looks like "evaluated nothing".
	if len(state.FitnessEvaluated) > 0 {
		attrs = append(attrs, attribute.StringSlice(AttrFitnessEvaluated, renderFitness(state.FitnessEvaluated)))
	}
	return attrs
}

// inferenceAttribution maps the decision mode to the inference.used /
// inference.fallback_reason pair. mode=llm falls back to the rules engine until
// the M4 LLM path exists, recording why; mode=rules (and any unknown mode) runs
// rules with no fallback. inference.configured is the model flag, set by the
// caller. Path-agnostic: once M4 lands, the llm branch returns ("llm", "").
func inferenceAttribution(mode string) (used, fallbackReason string) {
	if mode == "llm" {
		return InferenceRules, FallbackLLMNotImplemented
	}
	return InferenceRules, ""
}

// summarizeObjectives renders the objective weights as a stable sorted "k=v"
// summary (e.g. "balanced=0.3,endurance=0.7"); nil/empty keeps the "unset"
// placeholder for engines that carry no weights.
func summarizeObjectives(weights map[string]float64) string {
	if len(weights) == 0 {
		return DefaultObjectives
	}
	parts := make([]string, 0, len(weights))
	for k, v := range weights {
		parts = append(parts, k+"="+strconv.FormatFloat(v, 'g', -1, 64))
	}
	sort.Strings(parts)
	return strings.Join(parts, ",")
}

// renderFitness renders the evaluated fitness values as sorted "k=v" strings
// for the fitness.evaluated attribute; an empty map renders the empty slice
// (the schema attribute is always present).
func renderFitness(fitness map[string]float64) []string {
	out := make([]string, 0, len(fitness))
	for k, v := range fitness {
		out = append(out, k+"="+strconv.FormatFloat(v, 'g', -1, 64))
	}
	sort.Strings(out)
	return out
}

// allowedSummary renders the permission list for the interventions.allowed
// attribute and the feature_flag.result.variant event attribute. An empty
// allow-list (the fail-closed default) renders the explicit "none" marker —
// distinct from the "unrestricted" placeholder the #724 stub carried before
// flagd was wired.
func allowedSummary(allowed []string) string {
	if len(allowed) == 0 {
		return AllowedNone
	}
	return strings.Join(allowed, ",")
}

// disabledFlags is the fallback FlagSource: it always reports the agent disabled
// with empty permissions, so a nil flag source can never dispatch interventions.
type disabledFlags struct{}

func (disabledFlags) Evaluate(context.Context) flags.Snapshot {
	return flags.Snapshot{Enabled: flags.DefaultEnabled, Mode: flags.DefaultMode}
}

func derefStr(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

func derefFloat(p *float64) float64 {
	if p == nil {
		return 0
	}
	return *p
}
