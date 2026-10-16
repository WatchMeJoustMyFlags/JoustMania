package decision

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

// toggleRollout models actions.GatedRolloutActuator for the loop: DryRun reports
// the LIVE rollout_enabled gate, and SetControllerCount only records a write when
// the gate is on (dry run otherwise). The gate can be flipped mid-test, exactly as
// an operator flips rollout_enabled in agent.json.
type toggleRollout struct {
	fakeRollout
	dry atomic.Bool
}

func (r *toggleRollout) DryRun() bool { return r.dry.Load() }

func (r *toggleRollout) SetControllerCount(variant string) error {
	if r.dry.Load() {
		return nil // dry run: logged, nothing written
	}
	return r.fakeRollout.SetControllerCount(variant)
}

// UC: UC-045 A4 UC-050 A3 UC-070 A1
//
// A dry run (rollout_enabled off) must not commit state that only a real write
// commits. The observed stage stays where it is during a dry run (nothing was
// written), so the regression is only visible with a STATIONARY stage: before the
// fix, the first rehearsed expansion set lastWritten=1, every later cycle at the
// observed stage 0 was suppressed as "still in flight", and turning
// rollout_enabled back on never expanded until the agent restarted.
func TestInfraLoop_DryRunExpandThenEnableResumes(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &toggleRollout{}
	rollout.dry.Store(true)
	l, sr := newInfraLoop(t, rollout, &clock)

	for i := 0; i < 3; i++ {
		l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))
		clock = clock.Add(DefaultRolloutDwell)
	}
	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("dry run wrote %v, want nothing", got)
	}

	rollout.dry.Store(false)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz))

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("after re-enabling rollout_enabled writes = %v, want [one]", got)
	}
	spans := infraSpans(sr)
	want := []struct {
		action string
		dry    bool
	}{
		// Every dwell-paced dry-run cycle records the would-be expansion, not just the first.
		{RemediationExpand, true}, {RemediationExpand, true}, {RemediationExpand, true},
		{RemediationExpand, false},
	}
	if len(spans) != len(want) {
		t.Fatalf("got %d spans, want %d", len(spans), len(want))
	}
	for i, w := range want {
		action, _ := spanStr(spans[i], AttrRemediationAction)
		dry, _ := spanBool(spans[i], AttrRolloutDryRun)
		if action != w.action || dry != w.dry {
			t.Errorf("span[%d] = (%s, dry_run=%v), want (%s, dry_run=%v)", i, action, dry, w.action, w.dry)
		}
	}
}

// UC: UC-045 A4 UC-051 UC-070 A1
//
// A rehearsed rollback must not set rollbackInFlight / the cooldown: the stage
// never returns to 0 in a dry run, so before the fix the in-flight flag was never
// cleared and no real rollback followed after rollout_enabled was turned on.
func TestInfraLoop_DryRunRollbackThenEnableRollsBack(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &toggleRollout{}
	rollout.dry.Store(true)
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	// Dry run at a stationary active stage 3 with failing fitness.
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	if got := rollout.calls(); len(got) != 0 {
		t.Fatalf("dry run wrote %v, want nothing", got)
	}

	rollout.dry.Store(false)
	clock = clock.Add(time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	if got := rollout.calls(); len(got) != 1 || got[0] != "none" {
		t.Fatalf("after re-enabling rollout_enabled writes = %v, want [none]", got)
	}
	spans := infraSpans(sr)
	// Rehearsed rollback, then "none" while the rehearsal's cooldown runs (no
	// per-second rollback spam), then the real rollback once the gate is on.
	wantActions := []string{RemediationRollback, RemediationNone, RemediationRollback}
	if len(spans) != len(wantActions) {
		t.Fatalf("got %d spans, want %d", len(spans), len(wantActions))
	}
	for i, want := range wantActions {
		if action, _ := spanStr(spans[i], AttrRemediationAction); action != want {
			t.Errorf("span[%d] action = %q, want %q", i, action, want)
		}
	}
	if dry, _ := spanBool(spans[2], AttrRolloutDryRun); dry {
		t.Errorf("real rollback span rollout.dry_run = true, want false")
	}
}

// UC: UC-045 A4
//
// A dry-run rollback is re-recorded once its rehearsal cooldown has elapsed, so a
// long dry-run failure stays visible without logging every cycle.
func TestInfraLoop_DryRunRollbackRepeatsAfterCooldown(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &toggleRollout{}
	rollout.dry.Store(true)
	l, sr := newInfraLoop(t, rollout, &clock)
	l.SetRemediationSource(StaticRemediation(true))
	l.SetCooldown(30 * time.Second)

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))
	clock = clock.Add(31 * time.Second)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 3, clock, failingHz))

	spans := infraSpans(sr)
	if len(spans) != 2 {
		t.Fatalf("got %d spans, want 2", len(spans))
	}
	for i := range spans {
		if action, _ := spanStr(spans[i], AttrRemediationAction); action != RemediationRollback {
			t.Errorf("span[%d] action = %q, want rollback", i, action)
		}
	}
}

// UC: UC-045 A4
//
// The other direction: a real in-flight expansion is not forgotten when the gate
// is turned off. The reconcile anchor from the real write still applies, so the
// dry run does not rehearse re-writing a stage that is already in flight.
func TestInfraLoop_RealWriteStateSurvivesDryRun(t *testing.T) {
	clock := time.Unix(1000, 0)
	rollout := &toggleRollout{}
	l, sr := newInfraLoop(t, rollout, &clock)

	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz)) // real expand → one
	rollout.dry.Store(true)
	clock = clock.Add(DefaultRolloutDwell)
	l.OnInfraEvaluate(context.Background(), infraSnap("rust", 0, clock, passingHz)) // observation lags

	if got := rollout.calls(); len(got) != 1 || got[0] != "one" {
		t.Fatalf("writes = %v, want [one]", got)
	}
	spans := infraSpans(sr)
	if action, _ := spanStr(spans[1], AttrRemediationAction); action != RemediationNone {
		t.Errorf("lagging-observation dry cycle action = %q, want none", action)
	}
}

var _ RolloutActuator = (*toggleRollout)(nil)
