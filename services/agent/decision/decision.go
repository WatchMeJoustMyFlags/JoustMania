// Package decision holds the agent's decision loop: a gated, throttled hook that
// turns a GameContext snapshot into interventions via a pluggable rules engine
// and action sink, and emits the agent.span_received -> agent.decision ->
// agent.action audit trace for every evaluation that produces decisions
// (issue #724). ObjectiveRules (#726) is the objective-weighted rules engine;
// the action sink stays a no-op until the intervention API (#730).
//
// On every gated cycle the loop also evaluates the agent's OpenFeature control
// flags (issue #727) — never cached at startup — and applies them in order:
//
//	enabled               kill switch; false short-circuits the whole loop
//	mode                  selects the "rules" vs "llm" decision path
//	objectives            session-goal weights fed into the rules engine
//	interventions_allowed permission gate; decisions not on the allow-list are
//	                      blocked (decision.blocked=true on the span) before they
//	                      reach the action sink
//
// The objectives flag drives the engine through a LiveObjectives source: the
// loop publishes the per-cycle weights before running the rules, and the engine
// reads them inside Evaluate. When the flag yields no objectives the engine
// falls back to its DefaultObjectiveWeights (#726 config), so flagd being
// unreachable degrades to the deterministic {endurance: 1.0} baseline.
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

	// mu guards the log-throttle state below.
	mu      sync.Mutex
	lastLog time.Time
	now     func() time.Time
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

// OnEvaluate runs one evaluation pass:
//  1. evaluate the control flags (never cached);
//  2. kill switch — if enabled is false, return before any rules or spans;
//  3. publish the objectives flag into the rules engine;
//  4. emit a throttled (max 1/second) info log and run the rules;
//  5. when the rules return at least one decision, emit the full audit trace —
//     lazily, so idle evaluations cost no spans; the root span is backdated to
//     trig.T0 so its duration covers the whole Export processing;
//  6. per decision: gate on interventions_allowed (blocked decisions are
//     recorded with decision.blocked=true, never silently dropped) and apply the
//     permitted ones through the action sink.
func (l *Loop) OnEvaluate(ctx context.Context, c gamecontext.GameContext, trig EvalTrigger) {
	snapshot := l.Flags.Evaluate(ctx)

	// Kill switch: enabled=false short-circuits before any rules evaluation or
	// span emission. This is the safe default when flagd is unreachable.
	if !snapshot.Enabled {
		return
	}

	// Publish the per-cycle objectives flag into the rules engine. The engine
	// reads them inside Evaluate; engines that ignore objectives skip this.
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
		)
	}

	decisions := l.decide(ctx, snapshot, c)
	if len(decisions) == 0 {
		return // no decision, no trace
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

	allowed := snapshot.InterventionsAllowed
	for _, d := range decisions {
		l.runDecision(rootCtx, snapshot, d, allowed)
	}
}

// decide selects the decision path from snapshot.Mode and runs it. The "llm"
// path (M4) does not exist yet, so it logs a note and falls back to rules.
func (l *Loop) decide(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext) []Decision {
	switch snapshot.Mode {
	case "llm":
		l.Log.Info("agent.mode_llm_fallback",
			"note", "llm decision path not implemented (M4); falling back to rules")
		return l.Rules.Evaluate(ctx, c)
	default:
		// "rules" and any unrecognized mode use the deterministic rules path.
		return l.Rules.Evaluate(ctx, c)
	}
}

// runDecision emits one agent.decision span (full #724 schema) and its
// agent.action child, applying the decision unless its intervention is blocked
// by the interventions_allowed permission gate. Blocked actions are recorded,
// never silently dropped.
func (l *Loop) runDecision(ctx context.Context, snapshot flags.Snapshot, d Decision, allowed []string) {
	blocked := !snapshot.Permits(d.Intervention)

	dCtx, dSpan := l.Tracer.Start(ctx, SpanDecision,
		trace.WithAttributes(decisionAttributes(snapshot, d, blocked, allowed)...))
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
		// Blocked: not in the allow-list. Structured for span annotation (#729).
		l.Log.Warn("agent.decision_blocked",
			"blocked", true,
			"intervention", d.Intervention,
			"target_serial", d.TargetSerial,
			"reason", d.Reason,
			"allowed", allowed)
		return
	}
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

// decisionAttributes builds the complete #724 decision-span schema. Every span
// carries every attribute; subsystems that do not exist yet contribute
// explicit placeholder values (see telemetry.go). The mode, objectives, and
// interventions.allowed values now come from the evaluated flag snapshot (#727).
func decisionAttributes(snapshot flags.Snapshot, d Decision, blocked bool, allowed []string) []attribute.KeyValue {
	objective := d.ObjectiveServed
	if objective == "" {
		objective = DefaultObjectives
	}
	mode := snapshot.Mode
	if mode == "" {
		mode = DefaultMode
	}
	return []attribute.KeyValue{
		semconv.GenAIAgentName(AgentName),
		attribute.String(AttrMode, mode),
		attribute.String(AttrObjectives, summarizeObjectives(d.Objectives)),
		attribute.String(AttrInterventionsAllowed, allowedSummary(allowed)),
		attribute.String(AttrInferenceConfigured, DefaultInference),
		attribute.String(AttrInferenceUsed, DefaultInference),
		attribute.String(AttrInferenceFallback, ""),
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.String(AttrDecisionReason, d.Reason),
		attribute.String(AttrDecisionObjective, objective),
		attribute.Bool(AttrDecisionBlocked, blocked),
		attribute.StringSlice(AttrFitnessEvaluated, renderFitness(d.Fitness)),
	}
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
