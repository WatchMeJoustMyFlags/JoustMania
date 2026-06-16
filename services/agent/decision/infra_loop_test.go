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

// infraSnapStrategy is infraSnap with the observed rollout strategy set (#829).
func infraSnapStrategy(target string, stage int, now time.Time, hz float64, strategy string) infracontext.InfraContext {
	snap := infraSnap(target, stage, now, hz)
	snap.Window.RolloutStrategy = strategy
	return snap
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

// TestInfraLoop_ImmediateStrategySkipsLadder is the #829 guarantee: under
// strategy=immediate the controller-manager routes ALL controllers at once, so
// the count ladder is meaningless. Even with passing fitness at stage 0 (which
// would otherwise expand to "one"), the loop must NOT write and must record
// remediation.action="none" — but still emit the observe span.
func TestInfraLoop_ImmediateStrategySkipsLadder(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnapStrategy("rust", 0, clock, 30, "immediate"))

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none under strategy=immediate (no ladder climb)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1 (immediate still observes)", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationNone {
		t.Errorf("remediation.action = %q, want %q (observe-only under immediate)", v, RemediationNone)
	}
	// The expand-destination attribute must be absent (no expand happened).
	if _, ok := spanInt(spans[0], AttrRolloutControllerCount); ok {
		t.Errorf("rollout.controller_count present, want absent (no expand under immediate)")
	}
	// The observed strategy is surfaced on the span for the operator.
	if v, ok := spanStr(spans[0], AttrBluetoothRolloutStrategy); !ok || v != "immediate" {
		t.Errorf("bluetooth.rollout_strategy = %q (ok=%v), want %q", v, ok, "immediate")
	}
}

// TestInfraLoop_ProgressiveStrategyClimbsLadder is the explicit-progressive
// counterpart: strategy="progressive" behaves exactly like the ladder path.
func TestInfraLoop_ProgressiveStrategyClimbsLadder(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, _ := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnapStrategy("rust", 0, clock, 30, "progressive"))

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one] under strategy=progressive", got)
	}
}

// TestInfraLoop_AbsentStrategyClimbsLadder is the graceful-default guarantee
// (#829): an older controller-manager that omits bluetooth.rollout_strategy
// produces an empty strategy, which must behave exactly as before — the
// progressive ladder climbs. No regression.
func TestInfraLoop_AbsentStrategyClimbsLadder(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	// infraSnap leaves RolloutStrategy == "" (attribute absent on the span).
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one] when strategy absent (progressive ladder as before)", got)
	}
	spans := infraSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("got %d spans, want 1", len(spans))
	}
	if v, _ := spanStr(spans[0], AttrRemediationAction); v != RemediationExpand {
		t.Errorf("remediation.action = %q, want %q (absent strategy → ladder)", v, RemediationExpand)
	}
	// Strategy is present-but-empty on the span (schema-complete unknown).
	if v, ok := spanStr(spans[0], AttrBluetoothRolloutStrategy); !ok || v != "" {
		t.Errorf("bluetooth.rollout_strategy = %q (ok=%v), want present-but-empty", v, ok)
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

// TestInfraLoop_ReconcileSuppressesDoubleWrite is the #828 guarantee: after an
// expansion the observed RolloutCount lags for the full observe latency. With a
// dwell SHORTER than that latency, a naive loop would re-read NextStage(stale=0)
// and re-write the SAME stage ("one") a second time. The reconcile guard
// suppresses the expansion until the observation catches up to what we wrote.
func TestInfraLoop_ReconcileSuppressesDoubleWrite(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	// Short dwell so the dwell timer is NOT the thing blocking the re-expand — the
	// reconcile guard must carry correctness on its own.
	l.dwell = time.Second

	// First expansion: none(0) → one. lastWritten is now 1.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))
	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one]", got)
	}

	// Dwell elapses, but the observed stage is STILL the stale 0 (observation
	// lagging). Without the reconcile guard NextStage(0)=one would be re-written.
	clock = clock.Add(2 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))
	if got := rollout.calls(); len(got) != 1 {
		t.Fatalf("writes = %v, want only the first expansion (reconcile suppresses double-write)", got)
	}

	// Observation catches up to stage one(1): the loop may now expand one → three.
	clock = clock.Add(2 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 1, clock, 30))
	if got := rollout.calls(); len(got) != 2 || got[1] != "three" {
		t.Fatalf("writes = %v, want [one three] once observation caught up", got)
	}
	if n := len(infraSpans(sr)); n != 3 {
		t.Errorf("got %d spans, want 3 (one per active cycle)", n)
	}
}

// TestInfraLoop_ProgressiveClimbsOnFreshObservation confirms the reconcile guard
// does NOT block the normal climb: each cycle the observation reflects the prior
// write, so the ladder advances none→one→three→six as observed stages catch up.
func TestInfraLoop_ProgressiveClimbsOnFreshObservation(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, _ := newInfraLoop(t, rollout, &clock)
	l.dwell = time.Second

	stages := []int{0, 1, 3, 6}
	wantWrites := []string{"one", "three", "six", "all"}
	for i, stage := range stages {
		clock = clock.Add(2 * time.Second) // exceed dwell each cycle
		l.OnInfraEvaluate(context.Background(), infraSnap("rust", stage, clock, 30))
		if got := rollout.calls(); len(got) != i+1 {
			t.Fatalf("after observing stage %d, writes = %v, want %d total", stage, got, i+1)
		}
	}
	if got := rollout.calls(); len(got) != len(wantWrites) {
		t.Fatalf("writes = %v, want %v", got, wantWrites)
	}
	for i, w := range wantWrites {
		if rollout.calls()[i] != w {
			t.Fatalf("write[%d] = %q, want %q", i, rollout.calls()[i], w)
		}
	}
}

// TestInfraLoop_StaleObservationSkipped checks the optional freshness guard:
// an observation older than the configured bound is skipped (no write, no span).
func TestInfraLoop_StaleObservationSkipped(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetObserveStaleness(3 * time.Second)

	snap := infraSnap("rust", 0, clock, 30)
	snap.UpdatedAt = clock.Add(-10 * time.Second) // observed 10s ago, beyond the 3s bound
	l.OnInfraEvaluate(context.Background(), snap)

	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("writes = %v, want none (stale observation skipped)", got)
	}
	if n := len(infraSpans(sr)); n != 0 {
		t.Fatalf("got %d spans, want 0 (stale observation skipped before span)", n)
	}
}

// TestInfraLoop_AbsentTimestampNotBlocked is the graceful-default guarantee: a
// zero/absent UpdatedAt (test helpers, an older producer) must NOT block action
// even when the freshness guard is enabled.
func TestInfraLoop_AbsentTimestampNotBlocked(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, _ := newInfraLoop(t, rollout, &clock)
	l.SetObserveStaleness(3 * time.Second)

	// infraSnap leaves UpdatedAt zero; the guard must treat it as "behave as before".
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, 30))

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one] (absent timestamp must not block action)", got)
	}
}

// TestInfraLoop_FreshObservationActedOn confirms a within-bound observation is
// acted on when the freshness guard is enabled.
func TestInfraLoop_FreshObservationActedOn(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &fakeRollout{}
	l, _ := newInfraLoop(t, rollout, &clock)
	l.SetObserveStaleness(3 * time.Second)

	snap := infraSnap("rust", 0, clock, 30)
	snap.UpdatedAt = clock.Add(-1 * time.Second) // within the 3s bound
	l.OnInfraEvaluate(context.Background(), snap)

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one] (fresh observation acted on)", got)
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
