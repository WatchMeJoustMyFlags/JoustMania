package main

import (
	"context"
	"io"
	"log/slog"
	"testing"

	"github.com/joustmania/agent/actions"
	"github.com/joustmania/agent/decision"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// fakeGate is a static InterventionGate/RolloutGate for the wiring tests: it
// reports a constant gate value regardless of context, standing in for the live
// *flags.Flags.
type fakeGate bool

func (g fakeGate) InterventionsEnabled(context.Context) bool { return bool(g) }
func (g fakeGate) RolloutEnabled(context.Context) bool       { return bool(g) }

// TestActionSinkIsLiveGated: post-#1213 the sink wiring ALWAYS returns a
// GatedActionSink (the gate is read per Apply against the live flag), never nil and
// never a bare *Writer. The startup-time AGENT_INTERVENTIONS_ENABLED env gate is
// gone — the gate moved into the sink and is read live.
func TestActionSinkIsLiveGated(t *testing.T) {
	t.Setenv("INTERVENTIONS_FLAG_PATH", "/tmp/does-not-matter.json")
	sink := actionSink(fakeGate(true), discardLogger())
	if _, ok := sink.(*actions.GatedActionSink); !ok {
		t.Fatalf("expected *actions.GatedActionSink from the live-gated wiring, got %T", sink)
	}
}

// TestActionSinkGateOffSuppressesApply: with the live gate OFF (the fail-closed
// default state, and what a flagd outage resolves to) the gated sink applies
// nothing — exactly the inert-scaffold behavior the old env default produced.
func TestActionSinkGateOffSuppressesApply(t *testing.T) {
	t.Setenv("INTERVENTIONS_FLAG_PATH", "/tmp/does-not-matter.json")
	sink := actionSink(fakeGate(false), discardLogger())
	// A bad path would error on a real write; the gate-off path must short-circuit
	// BEFORE the writer is ever called, so Apply returns nil without touching disk.
	if err := sink.Apply(context.Background(), decision.Decision{Intervention: "audio_cue"}); err != nil {
		t.Fatalf("gate-off Apply should be a no-op, got error: %v", err)
	}
}

// TestRolloutActuatorIsLiveGated: post-#1213 the rollout wiring ALWAYS returns a
// GatedRolloutActuator (the gate is read per SetControllerCount/DryRun against the
// live flag), never a bare RolloutWriter or a fixed DryRunRolloutWriter.
func TestRolloutActuatorIsLiveGated(t *testing.T) {
	act := rolloutActuator(context.Background(), fakeGate(true), discardLogger())
	if _, ok := act.(*actions.GatedRolloutActuator); !ok {
		t.Fatalf("expected *actions.GatedRolloutActuator from the live-gated wiring, got %T", act)
	}
}

// TestRolloutActuatorDryRunTracksGate: DryRun() reflects the LIVE gate state, not a
// boot-time snapshot. Gate off (fail-closed default / flagd outage) ⇒ dry-run true;
// gate on ⇒ dry-run false.
func TestRolloutActuatorDryRunTracksGate(t *testing.T) {
	if off := rolloutActuator(context.Background(), fakeGate(false), discardLogger()); !off.DryRun() {
		t.Fatal("gate OFF must report DryRun()=true (rehearsal)")
	}
	if on := rolloutActuator(context.Background(), fakeGate(true), discardLogger()); on.DryRun() {
		t.Fatal("gate ON must report DryRun()=false (applied)")
	}
}
