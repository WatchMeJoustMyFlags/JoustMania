package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/gamecontext"
)

type fakeRules struct{ out []Decision }

func (f fakeRules) Evaluate(context.Context, gamecontext.GameContext) []Decision { return f.out }

type fakeSink struct {
	calls atomic.Int32
	err   error
}

func (f *fakeSink) Apply(context.Context, Decision) error {
	f.calls.Add(1)
	return f.err
}

type fakePerms struct{ list []string }

func (f fakePerms) Allowed() []string { return f.list }

// newTestLoop builds a Loop with a recording tracer and silent logger.
func newTestLoop(t *testing.T) (*Loop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	l := NewLoop(slog.New(slog.NewTextHandler(io.Discard, nil)))
	l.Tracer = tp.Tracer("test")
	return l, sr
}

func testTrigger() EvalTrigger {
	return EvalTrigger{
		Signal:     "metrics",
		RPCService: "opentelemetry.proto.collector.metrics.v1.MetricsService",
		T0:         time.Now().Add(-50 * time.Millisecond),
	}
}

func spansByName(spans []sdktrace.ReadOnlySpan, name string) []sdktrace.ReadOnlySpan {
	var out []sdktrace.ReadOnlySpan
	for _, s := range spans {
		if s.Name() == name {
			out = append(out, s)
		}
	}
	return out
}

func attrValue(span sdktrace.ReadOnlySpan, key string) (attribute.Value, bool) {
	for _, kv := range span.Attributes() {
		if string(kv.Key) == key {
			return kv.Value, true
		}
	}
	return attribute.Value{}, false
}

func TestOnEvaluate_NoDecisionsNoSpans(t *testing.T) {
	l, sr := newTestLoop(t)
	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())
	if n := len(sr.Ended()); n != 0 {
		t.Fatalf("spans = %d, want 0 (no decision, no trace)", n)
	}
}

func TestOnEvaluate_EmitsFullHierarchy(t *testing.T) {
	l, sr := newTestLoop(t)
	sink := &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "test"}}}
	l.Actions = sink
	trig := testTrigger()

	l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "session-1"}, trig)

	ended := sr.Ended()
	if len(ended) != 3 {
		t.Fatalf("spans = %d, want 3", len(ended))
	}
	roots := spansByName(ended, SpanReceived)
	decisions := spansByName(ended, SpanDecision)
	actions := spansByName(ended, SpanAction)
	if len(roots) != 1 || len(decisions) != 1 || len(actions) != 1 {
		t.Fatalf("hierarchy = %d/%d/%d, want 1/1/1", len(roots), len(decisions), len(actions))
	}

	root, dec, act := roots[0], decisions[0], actions[0]
	if root.Parent().IsValid() {
		t.Error("span_received must be the root span")
	}
	if dec.Parent().SpanID() != root.SpanContext().SpanID() {
		t.Error("agent.decision must be a child of agent.span_received")
	}
	if act.Parent().SpanID() != dec.SpanContext().SpanID() {
		t.Error("agent.action must be a child of agent.decision")
	}
	if root.SpanKind() != trace.SpanKindServer {
		t.Errorf("root kind = %v, want server", root.SpanKind())
	}
	if !root.StartTime().Equal(trig.T0) {
		t.Errorf("root start = %v, want backdated to t0 %v", root.StartTime(), trig.T0)
	}

	// rpc.* semconv on the root.
	if v, ok := attrValue(root, "rpc.service"); !ok || v.AsString() != trig.RPCService {
		t.Errorf("rpc.service = %v, want %s", v.AsString(), trig.RPCService)
	}

	// Full #724 schema with placeholders on the decision span.
	want := map[string]string{
		AttrMode:                 DefaultMode,
		AttrObjectives:           DefaultObjectives,
		AttrInterventionsAllowed: UnrestrictedAllowed,
		AttrInferenceConfigured:  DefaultInference,
		AttrInferenceUsed:        DefaultInference,
		AttrInferenceFallback:    "",
		AttrDecisionAction:       "noop",
		AttrDecisionReason:       "test",
		AttrDecisionObjective:    DefaultObjectives,
		"gen_ai.agent.name":      AgentName,
	}
	for key, expected := range want {
		if v, ok := attrValue(dec, key); !ok || v.AsString() != expected {
			t.Errorf("decision attr %s = %q (present=%v), want %q", key, v.AsString(), ok, expected)
		}
	}
	if v, ok := attrValue(dec, AttrDecisionBlocked); !ok || v.AsBool() {
		t.Errorf("decision.blocked = %v (present=%v), want false", v.AsBool(), ok)
	}
	if _, ok := attrValue(dec, AttrFitnessEvaluated); !ok {
		t.Error("fitness.evaluated must be present (empty placeholder)")
	}

	// feature_flag semconv event for the interventions.allowed evaluation.
	foundFlagEvent := false
	for _, ev := range dec.Events() {
		if ev.Name == "feature_flag" {
			foundFlagEvent = true
			keys := map[string]bool{}
			for _, kv := range ev.Attributes {
				keys[string(kv.Key)] = true
			}
			for _, k := range []string{"feature_flag.key", "feature_flag.provider.name", "feature_flag.result.variant"} {
				if !keys[k] {
					t.Errorf("feature_flag event missing %s", k)
				}
			}
		}
	}
	if !foundFlagEvent {
		t.Error("decision span must carry a feature_flag event")
	}

	if got := sink.calls.Load(); got != 1 {
		t.Errorf("sink calls = %d, want 1", got)
	}
}

func TestOnEvaluate_MultipleDecisions(t *testing.T) {
	l, sr := newTestLoop(t)
	sink := &fakeSink{}
	l.Rules = fakeRules{out: []Decision{
		{Intervention: "grant_shield", TargetSerial: "A", Reason: "balance"},
		{Intervention: "play_audio_cue", Reason: "ambience"},
	}}
	l.Actions = sink

	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	ended := sr.Ended()
	if len(spansByName(ended, SpanReceived)) != 1 ||
		len(spansByName(ended, SpanDecision)) != 2 ||
		len(spansByName(ended, SpanAction)) != 2 {
		t.Fatalf("spans = %d, want 1 root + 2 decisions + 2 actions", len(ended))
	}
	if got := sink.calls.Load(); got != 2 {
		t.Errorf("sink calls = %d, want 2", got)
	}
}

func TestOnEvaluate_BlockedDecisionRecordedNotApplied(t *testing.T) {
	l, sr := newTestLoop(t)
	sink := &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "eliminate_player", Reason: "test"}}}
	l.Actions = sink
	l.Perms = fakePerms{list: []string{"grant_shield", "play_audio_cue"}}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if got := sink.calls.Load(); got != 0 {
		t.Fatalf("sink calls = %d, want 0 (blocked action must not apply)", got)
	}
	ended := sr.Ended()
	if len(ended) != 3 {
		t.Fatalf("spans = %d, want 3 (blocked actions are recorded, not dropped)", len(ended))
	}
	dec := spansByName(ended, SpanDecision)[0]
	if v, ok := attrValue(dec, AttrDecisionBlocked); !ok || !v.AsBool() {
		t.Error("decision.blocked must be true on the decision span")
	}
	if v, ok := attrValue(dec, AttrInterventionsAllowed); !ok || v.AsString() != "grant_shield,play_audio_cue" {
		t.Errorf("interventions.allowed = %q, want the real list", v.AsString())
	}
	act := spansByName(ended, SpanAction)[0]
	if v, ok := attrValue(act, AttrDecisionBlocked); !ok || !v.AsBool() {
		t.Error("decision.blocked must be mirrored on the action span")
	}
}

func TestOnEvaluate_ApplyErrorRecorded(t *testing.T) {
	l, sr := newTestLoop(t)
	sink := &fakeSink{err: errors.New("flagd unreachable")}
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "test"}}}
	l.Actions = sink

	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	act := spansByName(sr.Ended(), SpanAction)[0]
	if act.Status().Code != codes.Error {
		t.Errorf("action status = %v, want error", act.Status().Code)
	}
	if v, ok := attrValue(act, "error.type"); !ok || v.AsString() != "*errors.errorString" {
		t.Errorf("error.type = %q, want the Go error class per semconv.ErrorType", v.AsString())
	}
}

func TestProbeRules_RateLimited(t *testing.T) {
	clock := time.Unix(1000, 0)
	p := NewProbeRules(5*time.Second, func() time.Time { return clock })

	if d := p.Evaluate(context.Background(), gamecontext.GameContext{}); len(d) != 1 {
		t.Fatalf("first probe = %d decisions, want 1", len(d))
	}
	if d := p.Evaluate(context.Background(), gamecontext.GameContext{}); d != nil {
		t.Fatalf("second probe within interval = %v, want nil", d)
	}
	clock = clock.Add(6 * time.Second)
	if d := p.Evaluate(context.Background(), gamecontext.GameContext{}); len(d) != 1 {
		t.Fatalf("probe after interval = %d decisions, want 1", len(d))
	}
}

// TestOnEvaluate_ConcurrentExportHandlers exercises the production call shape:
// the trace and metrics gRPC Export handlers invoke OnEvaluate from separate
// goroutines. Run under -race this guards the loop's shared throttle state.
func TestOnEvaluate_ConcurrentExportHandlers(t *testing.T) {
	l, sr := newTestLoop(t)
	sink := &fakeSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "noop", Reason: "race"}}}
	l.Actions = sink

	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())
		}()
	}
	wg.Wait()

	if got := len(spansByName(sr.Ended(), SpanReceived)); got != 50 {
		t.Fatalf("root spans = %d, want 50", got)
	}
}

func TestOnEvaluate_RendersFitnessAndObjectives(t *testing.T) {
	l, sr := newTestLoop(t)
	l.Rules = fakeRules{out: []Decision{{
		Intervention:    "adjust_music_tempo",
		Reason:          "test",
		ObjectiveServed: "accelerate",
		Fitness:         map[string]float64{"session_duration": 95, "target_session_seconds": 60},
		Objectives:      map[string]float64{"endurance": 0.7, "balanced": 0.3},
	}}}
	l.Actions = &fakeSink{}

	l.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	dec := spansByName(sr.Ended(), SpanDecision)[0]
	if v, ok := attrValue(dec, AttrObjectives); !ok || v.AsString() != "balanced=0.3,endurance=0.7" {
		t.Errorf("agent.objectives = %q, want sorted weight summary", v.AsString())
	}
	if v, ok := attrValue(dec, AttrDecisionObjective); !ok || v.AsString() != "accelerate" {
		t.Errorf("decision.objective_served = %q, want accelerate", v.AsString())
	}
	v, ok := attrValue(dec, AttrFitnessEvaluated)
	if !ok {
		t.Fatal("fitness.evaluated missing")
	}
	got := v.AsStringSlice()
	want := []string{"session_duration=95", "target_session_seconds=60"}
	if len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Errorf("fitness.evaluated = %v, want %v", got, want)
	}
}
