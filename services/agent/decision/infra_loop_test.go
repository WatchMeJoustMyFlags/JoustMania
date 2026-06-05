package decision

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/agent/infracontext"
)

// fakeRollout is an in-memory RolloutActuator: it records SetControllerCount
// calls and uses the real ladder semantics (so the loop walks the same stages as
// production). An err makes SetControllerCount fail.
type fakeRollout struct {
	mu      sync.Mutex
	written []string
	err     error
}

func (f *fakeRollout) SetControllerCount(variant string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.written = append(f.written, variant)
	return nil
}

func (f *fakeRollout) calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.written...)
}

// Ladder mirrors the production ladder (none→one→three→six→all).
func (f *fakeRollout) NextStage(current int) (string, int, bool) {
	ladder := [][2]int{{0, 1}, {1, 3}, {3, 6}, {6, 99}}
	names := map[int]string{1: "one", 3: "three", 6: "six", 99: "all"}
	for _, p := range ladder {
		if p[0] == current {
			return names[p[1]], p[1], true
		}
	}
	return "", 0, false
}

func (f *fakeRollout) StageVariantForValue(value int) (string, bool) {
	names := map[int]string{0: "none", 1: "one", 3: "three", 6: "six", 99: "all"}
	n, ok := names[value]
	return n, ok
}

// DryRun reports false: the fake applies its (in-memory) writes, mirroring the
// real RolloutWriter. dryRollout below overrides this to true.
func (f *fakeRollout) DryRun() bool { return false }

// dryRollout is a fakeRollout that reports DryRun()==true: it still records
// would-be writes (so dwell/ladder behavior matches), but the loop tags every
// span rollout.dry_run=true. Mirrors actions.DryRunRolloutWriter for the loop's
// purposes while keeping the in-memory call log.
type dryRollout struct{ fakeRollout }

func (d *dryRollout) DryRun() bool { return true }

// infraSnap builds an active-rollout InfraContext: a target backend, a rollout
// count (stage), one fresh controller, and a passing or failing window.
func infraSnap(target string, stage int, now time.Time, hz float64) infracontext.InfraContext {
	h := hz
	return infracontext.InfraContext{
		Window: infracontext.WindowSignals{
			EventGapMs:       fp(20),
			DroppedEventsPct: fp(0.01),
			MovementUpdateHz: &h,
			TargetBackend:    target,
			RolloutCount:     stage,
		},
		Controllers: map[string]*infracontext.ControllerHealth{
			"AA:BB": {Serial: "AA:BB", LastUpdate: now},
		},
	}
}

// newInfraLoop builds a loop with a fake clock, fake rollout actuator, default
// thresholds, an always-pass gate, and a recording tracer.
func newInfraLoop(t *testing.T, rollout RolloutActuator, clock *time.Time) (*InfraLoop, *tracetest.SpanRecorder) {
	t.Helper()
	sr := tracetest.NewSpanRecorder()
	tp := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(sr))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })

	l := NewInfraLoop(slog.New(slog.NewTextHandler(io.Discard, nil)), DefaultRolloutDwell, NewLiveBluetoothFitness(), rollout)
	l.tracer = tp.Tracer("test")
	l.now = func() time.Time { return *clock }
	l.SetGate(func(snap infracontext.InfraContext, now time.Time) bool { return true })
	return l, sr
}

func infraSpans(sr *tracetest.SpanRecorder) []sdktrace.ReadOnlySpan {
	return spansByName(sr.Ended(), SpanInfraDecision)
}

func spanStr(s sdktrace.ReadOnlySpan, key string) (string, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.AsString(), true
		}
	}
	return "", false
}

func spanInt(s sdktrace.ReadOnlySpan, key string) (int64, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.AsInt64(), true
		}
	}
	return 0, false
}

func spanBool(s sdktrace.ReadOnlySpan, key string) (bool, bool) {
	for _, kv := range s.Attributes() {
		if string(kv.Key) == key {
			return kv.Value.AsBool(), true
		}
	}
	return false, false
}

func TestInfraLoop_ExpandsWhenPassing(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// Stage none(0), passing fitness, dwell satisfied (never expanded): expand to one.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one]", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d decision spans, want 1", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationExpand {
		t.Errorf("remediation.action = %q, want %q", v, RemediationExpand)
	}
	if v, _ := spanStr(spans[0], AttrRolloutTarget); v != "rust" {
		t.Errorf("rollout.target = %q, want rust", v)
	}
	if v, _ := spanInt(spans[0], AttrRolloutControllerCount); v != 1 {
		t.Errorf("rollout.controller_count = %d, want 1", v)
	}
	if v, _ := spanStr(spans[0], AttrFitnessViolations); v != "" {
		t.Errorf("fitness.violations = %q, want empty", v)
	}
}

func TestInfraLoop_NoExpandWhenFitnessFailing(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// hz below the floor (10) → movement_update_hz violation → failing.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 5))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none (fitness failing → no rollback in PR E)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1 (failing cycle still spans)", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q (observe only)", v, RemediationNone)
	}
	if v, ok := spanStr(spans[0], AttrFitnessViolations); !ok || v == "" {
		t.Errorf("fitness.violations = %q, want non-empty", v)
	}
}

func TestInfraLoop_NoExpandAtTerminalStage(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// Stage all(99), passing: nothing above, no expansion, but still spans (active).
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 99, clock, 30))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none at terminal stage", got)
	}
	if spans := infraSpans(sr); len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	} else if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q", v, RemediationNone)
	}
}

func TestInfraLoop_DwellBlocksImmediateReExpand(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// First expansion: none→one.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))
	// Immediately re-evaluate at stage one(1): dwell not elapsed → no expansion.
	clock = clock.Add(5 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, 30))

	if got := rollout.calls(); len(got) != 1 {
		t.Fatalf("writes = %v, want only the first expansion (dwell blocks re-expand)", got)
	}

	// After the dwell elapses, the next evaluation expands one→three.
	clock = clock.Add(DefaultRolloutDwell)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, 30))
	if got := rollout.calls(); len(got) != 2 || got[1] != "three" {
		t.Fatalf("writes = %v, want second expansion to three after dwell", got)
	}
	if n := len(infraSpans(sr)); n != 3 {
		t.Errorf("got %d spans, want 3 (one per active cycle)", n)
	}
}

func TestInfraLoop_InactiveRolloutNoSpan(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// TargetBackend "" → rollout off → no decision span, no write.
	snap := infraSnap("", 0, clock, 30)
	l.OnInfraEvaluate(context.Background(), snap)

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none when rollout inactive", got)
	}
	if n := len(infraSpans(sr)); n != 0 {
		t.Fatalf("got %d spans, want 0 when rollout inactive", n)
	}
}

func TestInfraLoop_GatedOutNoSpan(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetGate(func(snap infracontext.InfraContext, now time.Time) bool { return false })

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	if len(rollout.calls()) != 0 || len(infraSpans(sr)) != 0 {
		t.Fatalf("gated-out cycle must not write or span")
	}
}

func TestInfraLoop_WriteErrorRecordedOnSpan(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{err: errors.New("disk full")}
	l, sr := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if status := spans[0].Status(); status.Code.String() != "Error" {
		t.Errorf("span status = %v, want Error", status.Code)
	}
	if n := len(spans[0].Events()); n == 0 {
		t.Errorf("write error not recorded as a span event")
	}
	// lastExpansion must NOT advance on a failed write, so the next passing cycle
	// retries the expansion.
	rollout.mu.Lock()
	rollout.err = nil
	rollout.mu.Unlock()
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))
	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("after failed write then success, writes = %v, want [one] (retry not dwell-blocked)", got)
	}
}

func TestInfraLoop_HoldExpansionSuppresses(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	// PR F hook: hold always on → no expansion even though everything else is clear.
	l.SetHoldExpansion(func(now time.Time) bool { return true })

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none while hold is in force", got)
	}
	if spans := infraSpans(sr); len(spans) != 1 {
		t.Fatalf("got %d spans, want 1 (still observes)", len(spans))
	} else if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q while held", v, RemediationNone)
	}
}

func TestInfraLoop_NilActuatorNeverExpands(t *testing.T) {
	clock := time.Unix(1000, 0)
	l, sr := newInfraLoop(t, nil, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1 (still observes with nil actuator)", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q (nil actuator never expands)", v, RemediationNone)
	}
}

// TestInfraLoop_DryRunAttributeReflectsActuator is the M3 trace-honesty test: the
// rollout.dry_run span attribute must distinguish a rehearsed decision from a
// real one. A real actuator → false; a dry-run actuator → true (the same
// remediation.action="expand" span otherwise); a nil actuator (never writes) →
// true.
func TestInfraLoop_DryRunAttributeReflectsActuator(t *testing.T) {
	cases := []struct {
		name    string
		rollout RolloutActuator
		want    bool
	}{
		{"real actuator", &fakeRollout{}, false},
		{"dry-run actuator", &dryRollout{}, true},
		{"nil actuator", nil, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := time.Unix(1000, 0)
			l, sr := newInfraLoop(t, tc.rollout, &clock)

			// Passing fitness at stage 0 → an expand decision (real/dry), or "none"
			// (nil), but the span is emitted on every active-rollout cycle either way.
			l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

			spans := infraSpans(sr)
			if len(spans) != 1 {
				t.Fatalf("got %d spans, want 1", len(spans))
			}
			got, ok := spanBool(spans[0], AttrRolloutDryRun)
			if !ok {
				t.Fatalf("rollout.dry_run absent; want present on every decision span")
			}
			if got != tc.want {
				t.Errorf("rollout.dry_run = %v, want %v", got, tc.want)
			}
		})
	}
}
