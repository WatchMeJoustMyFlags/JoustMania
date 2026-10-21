package promote

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

// tuning_source_test.go covers the #1788 use-time reads of the promotion timers
// (FR-150): promotion.revert_cooldown_seconds is resolved on every cooldown check,
// promotion.rewatch_interval_seconds / _timeout_seconds on every scheduled re-watch.

func TestCooldownSourceReadPerCheck(t *testing.T) {
	realDef := &recordingRealDefault{}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, fakeWatcher{degraded: false}, &recordingReverter{})
	now := time.Unix(2000, 0)
	p.SetClock(func() time.Time { return now })
	p.SetCooldown(time.Second) // a startup value the source must override
	var cooldown atomic.Int64
	cooldown.Store(int64(5 * time.Minute))
	p.SetCooldownSource(func() time.Duration { return time.Duration(cooldown.Load()) })
	p.SeedLastRevert(map[string]time.Time{promoteProposal.FlagKey: time.Unix(1900, 0)}) // 100 s ago

	cfg := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}
	if res := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence()); res.Outcome != OutcomeDiscarded {
		t.Fatalf("outcome = %q with a 5 min cooldown, want discarded", res.Outcome)
	}
	cooldown.Store(int64(time.Minute)) // the operator shortens the flag mid-run
	if res := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence()); res.Outcome == OutcomeDiscarded {
		t.Fatalf("outcome = %q after the cooldown flag dropped to 60 s, want applied (use-time read)", res.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Errorf("SetRealDefault calls = %d, want 1", len(realDef.calls))
	}
}

func TestCooldownSourceNonPositiveIsDefault(t *testing.T) {
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, &recordingRealDefault{}, fakeWatcher{}, &recordingReverter{})
	p.SetCooldownSource(func() time.Duration { return 0 })
	if got := p.effectiveCooldown(); got != DefaultRevertCooldown {
		t.Fatalf("effectiveCooldown = %v, want %v", got, DefaultRevertCooldown)
	}
}

func TestRewatcherResolvesTimersPerSchedule(t *testing.T) {
	var intervalReads, timeoutReads atomic.Int32
	rw := NewPollingRewatcherFunc(context.Background(),
		func() time.Duration { intervalReads.Add(1); return time.Millisecond },
		func() time.Duration { timeoutReads.Add(1); return time.Minute },
		discardLogger())
	if intervalReads.Load() != 0 || timeoutReads.Load() != 0 {
		t.Fatalf("timers read at construction (%d/%d), want at schedule time", intervalReads.Load(), timeoutReads.Load())
	}
	for i := 1; i <= 2; i++ {
		obs := &countingObserver{}
		obs.verdict.Store(int32(VerdictHealthy))
		rw.Schedule(context.Background(), "f", 0.5, time.Now(), obs, func(context.Context) {})
		waitFor(t, 5*time.Second, func() bool { return obs.calls.Load() >= 1 })
		if intervalReads.Load() != int32(i) || timeoutReads.Load() != int32(i) {
			t.Fatalf("after %d schedules: interval read %d, timeout read %d, want %d each", i, intervalReads.Load(), timeoutReads.Load(), i)
		}
	}
}
