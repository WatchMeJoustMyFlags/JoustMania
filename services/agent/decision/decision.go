// Package decision holds the agent's decision loop: a gated, throttled hook that
// turns a GameContext snapshot into interventions via a pluggable rules engine
// and action sink, and emits the agent.span_received -> agent.decision ->
// agent.action audit trace for every evaluation that produces decisions
// (issue #724). The concrete rules and actions are stubbed in this scaffold.
package decision

import (
	"context"
	"log/slog"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	semconv "go.opentelemetry.io/otel/semconv/v1.34.0"
	"go.opentelemetry.io/otel/trace"

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
	// (endurance/balanced/accelerate/chaos); empty until objectives exist
	// (#725/#731), recorded as decision.objective_served.
	ObjectiveServed string
}

// RulesEngine turns a context snapshot into zero or more Decisions. The
// context carries the active trace; an LLM-backed engine records its inference
// call as a gen_ai.* span (GenAI semantic conventions).
type RulesEngine interface {
	Evaluate(context.Context, gamecontext.GameContext) []Decision
}

// ActionSink applies one decision to the outside world. It is called once per
// decision inside that decision's agent.action span.
type ActionSink interface {
	Apply(context.Context, Decision) error
}

// Permissions reports the interventions the agent is allowed to apply.
// nil means "no restriction information" (unrestricted). The real
// implementation evaluates the interventions.allowed flag from flagd (#725);
// the scaffold uses UnrestrictedPermissions.
type Permissions interface {
	Allowed() []string
}

// UnrestrictedPermissions is the scaffold permission source: no restrictions.
type UnrestrictedPermissions struct{}

// Allowed reports no restriction information.
func (UnrestrictedPermissions) Allowed() []string { return nil }

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

// Loop wires the rules engine to the action sink and is invoked once per gated
// signal update.
type Loop struct {
	Rules   RulesEngine
	Actions ActionSink
	Perms   Permissions
	Log     *slog.Logger
	// Tracer produces the audit spans; tests inject a recording tracer.
	Tracer trace.Tracer

	lastLog time.Time
	now     func() time.Time
}

// NewLoop builds a Loop with the no-op rules/actions stubs, unrestricted
// permissions, and the global tracer. log may be nil, in which case
// slog.Default() is used.
func NewLoop(log *slog.Logger) *Loop {
	if log == nil {
		log = slog.Default()
	}
	return &Loop{
		Rules:   NoopRules{},
		Actions: NoopActions{},
		Perms:   UnrestrictedPermissions{},
		Log:     log,
		Tracer:  otel.Tracer(instrumentationName),
		now:     time.Now,
	}
}

// OnEvaluate runs one evaluation pass: emit a throttled (max 1/second) info
// log, run the rules, and apply any resulting decisions. When the rules return
// at least one decision, the full audit trace is emitted — lazily, so idle
// evaluations cost no spans; the root span is backdated to trig.T0 so its
// duration covers the whole Export processing.
func (l *Loop) OnEvaluate(ctx context.Context, c gamecontext.GameContext, trig EvalTrigger) {
	now := l.now
	if now == nil {
		now = time.Now
	}
	if t := now(); t.Sub(l.lastLog) >= throttleInterval {
		l.lastLog = t
		l.Log.Info("agent.evaluate",
			"session_id", c.SessionID,
			"player_count", len(c.Players),
			"game_mode", derefStr(c.Session.GameMode),
			"duration", derefFloat(c.Session.DurationSeconds),
		)
	}

	decisions := l.Rules.Evaluate(ctx, c)
	if len(decisions) == 0 {
		return // no decision, no trace
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

	allowed := l.permissions().Allowed()
	for _, d := range decisions {
		l.runDecision(rootCtx, d, allowed)
	}
}

// runDecision emits one agent.decision span (full #724 schema) and its
// agent.action child, applying the decision unless it is blocked by the
// permission list. Blocked actions are recorded, never silently dropped.
func (l *Loop) runDecision(ctx context.Context, d Decision, allowed []string) {
	blocked := !permits(allowed, d.Intervention)

	dCtx, dSpan := l.Tracer.Start(ctx, SpanDecision,
		trace.WithAttributes(decisionAttributes(d, blocked, allowed)...))
	defer dSpan.End()

	// The interventions.allowed evaluation as a feature_flag.* span event
	// (semconv) — same shape the openfeature OTel hooks emit in the Python
	// services. The stub provider is replaced by flagd in #725.
	dSpan.AddEvent("feature_flag", trace.WithAttributes(
		semconv.FeatureFlagKey(interventionsAllowedFlagKey),
		semconv.FeatureFlagProviderName(flagProviderStub),
		semconv.FeatureFlagResultVariant(allowedSummary(allowed)),
	))

	_, aSpan := l.Tracer.Start(dCtx, SpanAction, trace.WithAttributes(
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.Bool(AttrDecisionBlocked, blocked),
	))
	defer aSpan.End()

	if blocked {
		l.Log.Warn("agent.action_blocked",
			"intervention", d.Intervention, "allowed", allowed)
		return
	}
	if err := l.Actions.Apply(dCtx, d); err != nil {
		aSpan.RecordError(err)
		aSpan.SetAttributes(semconv.ErrorTypeKey.String("apply_failed"))
		aSpan.SetStatus(codes.Error, err.Error())
		l.Log.Error("agent.apply_failed",
			"error", err, "intervention", d.Intervention)
	}
}

// permissions returns the configured permission source, defaulting to
// unrestricted when nil.
func (l *Loop) permissions() Permissions {
	if l.Perms == nil {
		return UnrestrictedPermissions{}
	}
	return l.Perms
}

// decisionAttributes builds the complete #724 decision-span schema. Every span
// carries every attribute; subsystems that do not exist yet contribute
// explicit placeholder values (see telemetry.go).
func decisionAttributes(d Decision, blocked bool, allowed []string) []attribute.KeyValue {
	objective := d.ObjectiveServed
	if objective == "" {
		objective = DefaultObjectives
	}
	return []attribute.KeyValue{
		semconv.GenAIAgentName(AgentName),
		attribute.String(AttrMode, DefaultMode),
		attribute.String(AttrObjectives, DefaultObjectives),
		attribute.String(AttrInterventionsAllowed, allowedSummary(allowed)),
		attribute.String(AttrInferenceConfigured, DefaultInference),
		attribute.String(AttrInferenceUsed, DefaultInference),
		attribute.String(AttrInferenceFallback, ""),
		attribute.String(AttrDecisionAction, d.Intervention),
		attribute.String(AttrDecisionReason, d.Reason),
		attribute.String(AttrDecisionObjective, objective),
		attribute.Bool(AttrDecisionBlocked, blocked),
		attribute.StringSlice(AttrFitnessEvaluated, []string{}),
	}
}

// permits reports whether the intervention is allowed. A nil list means no
// restriction information (everything permitted).
func permits(allowed []string, intervention string) bool {
	if allowed == nil {
		return true
	}
	for _, a := range allowed {
		if a == intervention {
			return true
		}
	}
	return false
}

// allowedSummary renders the permission list for the interventions.allowed
// attribute and the feature_flag.result.variant event attribute.
func allowedSummary(allowed []string) string {
	if allowed == nil {
		return UnrestrictedAllowed
	}
	return strings.Join(allowed, ",")
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
