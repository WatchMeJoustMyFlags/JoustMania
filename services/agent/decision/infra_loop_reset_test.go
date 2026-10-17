package decision

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"
)

// servedRollout is a fakeRollout that also reads back the served stage
// (RolloutStageReader). By default it serves the agent's own last write; a test
// overrides that with override() to model an operator reset or a flag-service
// restart, or with fail() to model an unreachable flag service.
type servedRollout struct {
	fakeRollout

	smu      sync.Mutex
	epoch    string
	override *ServedRolloutStage
	err      error
	reads    int
}

func newServedRollout() *servedRollout { return &servedRollout{epoch: "e1"} }

func (s *servedRollout) ServedStage() (ServedRolloutStage, error) {
	s.smu.Lock()
	defer s.smu.Unlock()
	s.reads++
	if s.err != nil {
		return ServedRolloutStage{}, s.err
	}
	if s.override != nil {
		return *s.override, nil
	}
	values := map[string]int{"none": 0, "one": 1, "three": 3, "six": 6, "all": 99}
	calls := s.calls()
	if len(calls) == 0 {
		return ServedRolloutStage{Value: 0, OnLadder: true, Epoch: s.epoch}, nil
	}
	return ServedRolloutStage{Value: values[calls[len(calls)-1]], OnLadder: true, ByAgent: true, Epoch: s.epoch}, nil
}

func (s *servedRollout) serve(st ServedRolloutStage) {
	s.smu.Lock()
	defer s.smu.Unlock()
	s.override = &st
}

func (s *servedRollout) fail(err error) {
	s.smu.Lock()
	defer s.smu.Unlock()
	s.err = err
}

func (s *servedRollout) readCount() int {
	s.smu.Lock()
	defer s.smu.Unlock()
	return s.reads
}

// An operator ends the rollout (strategy off → no target backend observed) after
// the agent expanded. The next rollout starts fresh: the first expansion is not
// blocked by the stage the agent wrote in the previous run, even with an
// actuator that cannot read the served stage back.
// UC: UC-050 BR-001
// UC: UC-070 A1
func TestInfraLoop_InactiveRolloutResetsState(t *testing.T) {
	t.Run("UC-050/BR-001 expansion after a manual reset needs no agent restart", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := &fakeRollout{}
		l, _ := newInfraLoop(t, rollout, &clock)

		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // expand → one
		clock = clock.Add(2 * time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("", 0, clock, passingHz)) // operator: strategy off
		clock = clock.Add(2 * time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // next run

		if got := rollout.calls(); len(got) != 2 || got[1] != "one" {
			t.Fatalf("writes = %v, want [one one] (the second run expands at once)", got)
		}
	})
}

// After a real rollback the cooldown runs; the operator ends the rollout and
// starts the next one inside the cooldown. The cooldown belonged to the old run,
// so the new one expands at once.
// UC: UC-051 A2
// UC: UC-070 A1
func TestInfraLoop_InactiveRolloutClearsCooldown(t *testing.T) {
	t.Run("UC-051/A2 a manual reset clears the cooldown like an agent restart", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := &fakeRollout{}
		l, sr := newInfraLoop(t, rollout, &clock)
		l.SetRemediationSource(StaticRemediation(true))
		l.SetCooldown(30 * time.Second)

		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 1, clock, failingHz)) // rollback → none
		clock = clock.Add(2 * time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("", 0, clock, passingHz)) // reset
		clock = clock.Add(2 * time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz))

		if got := rollout.calls(); len(got) != 2 || got[0] != "none" || got[1] != "one" {
			t.Fatalf("writes = %v, want [none one] (no cooldown carried into the new rollout)", got)
		}
		spans := infraSpans(sr)
		if v, _ := spanStr(spans[len(spans)-1], AttrRemediationAction); v != RemediationExpand {
			t.Errorf("last action = %q, want %q", v, RemediationExpand)
		}
	})
}

// The operator resets only current_controller_count (target and strategy
// unchanged) while the agent waits for its last expansion to show up. The flag
// service no longer serves the agent's write, so the loop forgets it and climbs
// from the observed stage again.
// UC: UC-050 BR-001
// UC: UC-070 A1
func TestInfraLoop_ExternalStageResetUnblocksExpansion(t *testing.T) {
	cases := []struct {
		name   string
		served ServedRolloutStage
	}{
		{"UC-050/BR-001 operator wrote none", ServedRolloutStage{Value: 0, OnLadder: true, ByAgent: false, Epoch: "e1"}},
		{"UC-050/BR-001 overlay cleared, baseline served", ServedRolloutStage{Value: 0, OnLadder: true, Epoch: "e1"}},
		{"UC-050/BR-001 agent overlay but another value", ServedRolloutStage{Value: 3, OnLadder: true, ByAgent: true, Epoch: "e1"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			clock := time.Unix(1000, 0)
			rollout := newServedRollout()
			l, _ := newInfraLoop(t, rollout, &clock)

			l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // expand → one
			rollout.serve(tc.served)
			clock = clock.Add(time.Minute) // past the dwell
			l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz))

			if got := rollout.calls(); len(got) != 2 || got[1] != "one" {
				t.Fatalf("writes = %v, want [one one] (the reset stage is climbed again)", got)
			}
		})
	}
}

// While the flag service still serves the agent's own write, a lagging
// observation keeps suppressing the double write (#828): the served-stage check
// only clears state the flag service contradicts.
// UC: UC-050 main
func TestInfraLoop_ServedOwnWriteKeepsReconcileGuard(t *testing.T) {
	t.Run("UC-050/main lagging observation of the agent's own write is not re-written", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := newServedRollout()
		l, _ := newInfraLoop(t, rollout, &clock)

		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // expand → one
		clock = clock.Add(time.Minute)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // still observes 0

		if got := rollout.calls(); len(got) != 1 {
			t.Fatalf("writes = %v, want [one] (no double write while served = own write)", got)
		}
		if rollout.readCount() == 0 {
			t.Fatal("served stage never read while the observation lagged")
		}
	})
}

// The flag service restarts: a new boot epoch, every overlay gone. The agent's
// record of its write is void and the rollout climbs again from what is served.
// UC: UC-050 BR-001
// UC: UC-051 A2
func TestInfraLoop_FlagServiceRestartResetsState(t *testing.T) {
	t.Run("UC-051/A2 new epoch clears the last write and the cooldown", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := newServedRollout()
		l, _ := newInfraLoop(t, rollout, &clock)
		l.SetRemediationSource(StaticRemediation(true))
		l.SetCooldown(30 * time.Second)

		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 1, clock, failingHz)) // rollback → none
		clock = clock.Add(time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // cooldown, epoch e1 seen
		if got := rollout.calls(); len(got) != 1 {
			t.Fatalf("writes = %v, want [none] (cooldown holds while e1 serves the agent's none)", got)
		}

		// Same served value, still marked as the agent's, but a new epoch: only the
		// epoch tells the restart apart.
		rollout.serve(ServedRolloutStage{Value: 0, OnLadder: true, ByAgent: true, Epoch: "e2"})
		clock = clock.Add(time.Second)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz))

		if got := rollout.calls(); len(got) != 2 || got[1] != "one" {
			t.Fatalf("writes = %v, want [none one] (the restart voided the cooldown)", got)
		}
	})
}

// An unreadable flag service never unblocks anything: the state is kept (fail
// safe) and the observation guard keeps holding the expansion.
// UC: UC-050 BR-001
func TestInfraLoop_ServedStageUnreadableKeepsState(t *testing.T) {
	t.Run("UC-050/BR-001 read error keeps the last write", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := newServedRollout()
		l, _ := newInfraLoop(t, rollout, &clock)

		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz)) // expand → one
		rollout.fail(errors.New("flag service down"))
		clock = clock.Add(time.Minute)
		l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 0, clock, passingHz))

		if got := rollout.calls(); len(got) != 1 {
			t.Fatalf("writes = %v, want [one] (no write while the served stage is unknown)", got)
		}
	})
}

// At a caught-up stage nothing blocks, so the loop does not read the flag
// service every cycle.
// UC: UC-050 main
func TestInfraLoop_ServedStageReadOnlyWhenBlocked(t *testing.T) {
	t.Run("UC-050/main no read at a caught-up stage", func(t *testing.T) {
		clock := time.Unix(1000, 0)
		rollout := newServedRollout()
		l, _ := newInfraLoop(t, rollout, &clock)
		l.SetHoldExpansion(func(time.Time) bool { return true }) // observe only

		for i := 0; i < 3; i++ {
			l.OnInfraEvaluate(context.Background(), infraSnap("unstable", 3, clock, passingHz))
			clock = clock.Add(time.Second)
		}
		if n := rollout.readCount(); n != 0 {
			t.Fatalf("served stage read %d times, want 0", n)
		}
	})
}
