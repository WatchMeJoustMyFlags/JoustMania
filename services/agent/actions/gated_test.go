package actions

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/infracontext"
)

// staticGate is a constant InterventionGate/RolloutGate for the gated-wrapper unit
// tests.
type staticGate bool

func (g staticGate) InterventionsEnabled(context.Context) bool { return bool(g) }
func (g staticGate) RolloutEnabled(context.Context) bool       { return bool(g) }

// recordingSink records whether Apply was called (the real Writer stand-in for the
// intervention-gate tests).
type recordingSink struct {
	applied bool
	err     error
}

func (r *recordingSink) Apply(context.Context, decision.Decision) error {
	r.applied = true
	return r.err
}

// TestGatedActionSink_GateOff_NoApply: with the gate OFF (the fail-closed default,
// and what a flagd outage resolves to) the gated sink does NOT call the wrapped
// writer and returns nil — a decided, fully-gated intervention is never written.
func TestGatedActionSink_GateOff_NoApply(t *testing.T) {
	inner := &recordingSink{}
	sink := NewGatedActionSink(staticGate(false), inner, nil)
	if err := sink.Apply(context.Background(), decision.Decision{Intervention: "audio_cue"}); err != nil {
		t.Fatalf("gate-off Apply must be a no-op, got %v", err)
	}
	if inner.applied {
		t.Fatal("gate-off Apply must NOT call the wrapped writer")
	}
}

// TestGatedActionSink_GateOn_Delegates: with the gate ON the gated sink delegates to
// the wrapped writer (and propagates its error).
func TestGatedActionSink_GateOn_Delegates(t *testing.T) {
	sentinel := errors.New("write failed")
	inner := &recordingSink{err: sentinel}
	sink := NewGatedActionSink(staticGate(true), inner, nil)
	if err := sink.Apply(context.Background(), decision.Decision{Intervention: "audio_cue"}); !errors.Is(err, sentinel) {
		t.Fatalf("gate-on Apply must delegate (and propagate the writer error), got %v", err)
	}
	if !inner.applied {
		t.Fatal("gate-on Apply must call the wrapped writer")
	}
}

// TestGatedRolloutActuator_GateOff_DryRun: with the gate OFF SetControllerCount is a
// dry-run (no write — it tolerates a non-existent path) and DryRun() reports true.
func TestGatedRolloutActuator_GateOff_DryRun(t *testing.T) {
	// A path that does NOT exist: the real writer would error on a read-modify-write,
	// so a nil error proves the gate short-circuited to the dry-run path.
	w := NewRolloutWriter("/nonexistent/dir/rollout.json", nil)
	act := NewGatedRolloutActuator(context.Background(), staticGate(false), w, nil)
	if !act.DryRun() {
		t.Fatal("gate OFF must report DryRun()=true")
	}
	if err := act.SetControllerCount(StageOneVariant); err != nil {
		t.Fatalf("gate-off SetControllerCount must be a dry-run no-op, got %v", err)
	}
}

// TestGatedRolloutActuator_GateOn_Applies: with the gate ON DryRun() reports false
// and SetControllerCount delegates to the real writer (which errors on a bad path,
// proving the write was actually attempted rather than rehearsed).
func TestGatedRolloutActuator_GateOn_Applies(t *testing.T) {
	w := NewRolloutWriter("/nonexistent/dir/rollout.json", nil)
	act := NewGatedRolloutActuator(context.Background(), staticGate(true), w, nil)
	if act.DryRun() {
		t.Fatal("gate ON must report DryRun()=false")
	}
	if err := act.SetControllerCount(StageOneVariant); err == nil {
		t.Fatal("gate-on SetControllerCount must attempt the real write (and error on the bad path)")
	}
}

// TestGatedRolloutActuator_LadderDelegates: the ladder helpers delegate to the
// wrapped writer regardless of gate state, so the value↔variant mapping stays
// single-sourced.
func TestGatedRolloutActuator_LadderDelegates(t *testing.T) {
	act := NewGatedRolloutActuator(context.Background(), staticGate(false), NewRolloutWriter("", nil), nil)
	if v, val, ok := act.NextStage(StageNoneValue); !ok || v != StageOneVariant || val != StageOneValue {
		t.Fatalf("NextStage(none) = (%q,%d,%v), want (one,1,true)", v, val, ok)
	}
	if v, ok := act.StageVariantForValue(StageThreeValue); !ok || v != StageThreeVariant {
		t.Fatalf("StageVariantForValue(3) = (%q,%v), want (three,true)", v, ok)
	}
}

// flippingGate answers RolloutEnabled from a script, one answer per call, so a
// test can model rollout_enabled flipping between two reads.
type flippingGate struct{ answers []bool }

func (g *flippingGate) InterventionsEnabled(context.Context) bool { return false }
func (g *flippingGate) RolloutEnabled(context.Context) bool {
	v := g.answers[0]
	if len(g.answers) > 1 {
		g.answers = g.answers[1:]
	}
	return v
}

// UC: UC-045 A4
//
// ApplyControllerCount reads the gate once and reports what actually happened, so
// the InfraLoop never commits state for a write that was only rehearsed.
func TestGatedRolloutActuator_ApplyReportsOutcome(t *testing.T) {
	w, path := newTestRolloutWriter(t)
	before := defaultVariantOf(t, path, flagCurrentControllerCount)

	off := NewGatedRolloutActuator(context.Background(), staticGate(false), w, nil)
	if applied, err := off.ApplyControllerCount(StageOneVariant); err != nil || applied {
		t.Fatalf("gate off: (applied=%v, err=%v), want (false, nil)", applied, err)
	}
	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != before {
		t.Fatalf("gate off wrote %q, want unchanged %q", got, before)
	}

	on := NewGatedRolloutActuator(context.Background(), staticGate(true), w, nil)
	if applied, err := on.ApplyControllerCount(StageOneVariant); err != nil || !applied {
		t.Fatalf("gate on: (applied=%v, err=%v), want (true, nil)", applied, err)
	}
	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != StageOneVariant {
		t.Fatalf("gate on wrote %q, want %q", got, StageOneVariant)
	}
}

// UC: UC-045 A4
//
// End to end with the real gated actuator and rollout.json: a dry run at a
// stationary stage, then rollout_enabled on, then the loop expands for real
// without an agent restart.
func TestGatedRolloutActuator_LoopResumesAfterDryRun(t *testing.T) {
	w, path := newTestRolloutWriter(t)
	gate := &flippingGate{answers: []bool{false}}
	act := NewGatedRolloutActuator(context.Background(), gate, w, nil)

	loop := decision.NewInfraLoop(discardLogger(), time.Hour, nil, act)
	loop.SetGate(func(infracontext.InfraContext, time.Time) bool { return true })
	snap := func() infracontext.InfraContext {
		hz := 30.0
		gap, dropped := 20.0, 0.01
		return infracontext.InfraContext{Window: infracontext.WindowSignals{
			EventGapMs: &gap, DroppedEventsPct: &dropped, MovementUpdateHz: &hz,
			TargetBackend: "rust", RolloutCount: StageNoneValue,
		}}
	}
	for i := 0; i < 3; i++ {
		loop.OnInfraEvaluate(context.Background(), snap())
	}
	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != StageNoneVariant {
		t.Fatalf("dry run wrote %q, want %q", got, StageNoneVariant)
	}

	gate.answers = []bool{true}
	loop.OnInfraEvaluate(context.Background(), snap())
	if got := defaultVariantOf(t, path, flagCurrentControllerCount); got != StageOneVariant {
		t.Fatalf("after rollout_enabled on: current_controller_count = %q, want %q", got, StageOneVariant)
	}
}
