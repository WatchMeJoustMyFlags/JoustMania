package actions

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/decision"
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
