package promote

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment"
)

// live_test.go covers the LIVE-wired safety-net pieces (#1057/#1065): the
// store-backed RealGameFitness source, the post-revert COOLDOWN that prevents
// re-promotion thrash (item 3), the tri-state Observe + background RE-WATCH of an
// insufficient-games apply (item 4).

// --- RealGameFitness over the #965 FitnessStore (#1057 item 2) ---

func TestStoreRealGameFitnessReadsRecentReal(t *testing.T) {
	store := experiment.NewFitnessStore(0)
	started := time.Unix(1000, 0)
	// Two real games for "endurance" and one shadow game (must be ignored).
	store.Record(experiment.FitnessSample{GameID: "g1", Fitness: 0.7, Objective: "endurance", GameKind: experiment.GameKindReal, StartedAt: started})
	store.Record(experiment.FitnessSample{GameID: "g2", Fitness: 0.6, Objective: "endurance", GameKind: experiment.GameKindReal, StartedAt: started})
	store.Record(experiment.FitnessSample{GameID: "s1", Fitness: 0.1, Objective: "endurance", GameKind: "shadow", StartedAt: started})

	src := NewStoreRealGameFitness(store, func(string) string { return "endurance" })
	got, err := src.Recent(context.Background(), "death_grace_period_ms", started.Add(-time.Second), 5)
	if err != nil {
		t.Fatalf("Recent err = %v", err)
	}
	if len(got) != 2 {
		t.Fatalf("got %d real samples, want 2 (shadow excluded)", len(got))
	}
}

func TestStoreRealGameFitnessNilStoreYieldsNoSamples(t *testing.T) {
	src := NewStoreRealGameFitness(nil, nil)
	got, err := src.Recent(context.Background(), "f", time.Time{}, 5)
	if err != nil || len(got) != 0 {
		t.Fatalf("nil store: got (%v, %v), want (nil, no error)", got, err)
	}
}

// --- tri-state Observe (#1065 item 4 foundation) ---

func TestObserveTriState(t *testing.T) {
	policy := WatchPolicy{MinGames: 3, DegradationMargin: 0.05, WindowGames: 5}
	const baseline = 0.70
	cases := []struct {
		name    string
		samples []float64
		want    WatchVerdict
	}{
		{"insufficient", []float64{0.1, 0.1}, VerdictInsufficient},
		{"healthy", []float64{0.70, 0.71, 0.69}, VerdictHealthy},
		{"degraded", []float64{0.40, 0.42, 0.41}, VerdictDegraded},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			w := NewFitnessWatcher(fakeRealGameFitness{samples: tc.samples}, policy, discardLogger())
			got, err := w.Observe(context.Background(), "f", baseline, time.Time{})
			if err != nil {
				t.Fatalf("Observe err = %v", err)
			}
			if got != tc.want {
				t.Errorf("Observe = %v, want %v", got, tc.want)
			}
		})
	}
}

// --- post-revert COOLDOWN (#1065 item 3) ---

// TestRevertCooldownSuppressesImmediateRepromotion: after a fail-safe revert (driven
// by a telemetry error), an immediate autonomous re-promotion of the SAME flag is
// suppressed (recorded no-op) while the cooldown holds — breaking the
// err→revert→re-promote→err thrash loop.
func TestRevertCooldownSuppressesImmediateRepromotion(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	// Watcher always errors → every apply fail-safe reverts.
	erroring := fakeWatcher{err: errTelemetry}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, erroring, rev)

	// Drive the clock manually so the test is deterministic.
	now := time.Unix(1000, 0)
	p.SetClock(func() time.Time { return now })
	p.SetCooldown(5 * time.Minute)

	cfg := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}

	// First promotion: applies, watch errors, reverts (sets cooldown at t=1000).
	res1 := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence())
	if res1.Outcome != OutcomeReverted {
		t.Fatalf("first outcome = %q, want reverted", res1.Outcome)
	}
	if rev.calls != 1 {
		t.Fatalf("revert calls = %d, want 1", rev.calls)
	}

	// Immediate re-promotion (still t=1000): suppressed by cooldown, no new apply.
	res2 := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence())
	if res2.Outcome != OutcomeDiscarded {
		t.Fatalf("re-promotion outcome = %q, want discarded (cooldown)", res2.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Errorf("SetRealDefault calls = %d, want 1 (re-apply suppressed by cooldown)", len(realDef.calls))
	}

	// After the cooldown elapses, a re-promotion is allowed again (applies + reverts).
	now = now.Add(6 * time.Minute)
	res3 := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence())
	if res3.Outcome != OutcomeReverted {
		t.Fatalf("post-cooldown outcome = %q, want reverted (allowed again)", res3.Outcome)
	}
	if len(realDef.calls) != 2 {
		t.Errorf("SetRealDefault calls = %d, want 2 (re-apply after cooldown)", len(realDef.calls))
	}
}

// TestSeedLastRevertPreservesCooldownAcrossRestart: a post-revert cooldown stamp
// recovered from the durable store (#1076 item 3) must suppress an immediate
// re-promotion after a restart — modeling an agent that reverted, then restarted, then
// is asked to re-promote the same flag inside the cooldown window.
func TestSeedLastRevertPreservesCooldownAcrossRestart(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &recordingReverter{}
	healthy := fakeWatcher{degraded: false} // would normally apply + keep
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, healthy, rev)

	now := time.Unix(2000, 0)
	p.SetClock(func() time.Time { return now })
	p.SetCooldown(5 * time.Minute)

	// Simulate a pre-restart revert at t=1900 recovered from the durable store.
	p.SeedLastRevert(map[string]time.Time{promoteProposal.FlagKey: time.Unix(1900, 0)})

	cfg := Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}

	// Within the seeded cooldown (1900 + 5m > 2000): re-promotion is suppressed.
	res := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence())
	if res.Outcome != OutcomeDiscarded {
		t.Fatalf("re-promotion outcome = %q, want discarded (seeded cooldown holds)", res.Outcome)
	}
	if len(realDef.calls) != 0 {
		t.Errorf("SetRealDefault calls = %d, want 0 (apply suppressed by restored cooldown)", len(realDef.calls))
	}

	// Past the seeded cooldown: a re-promotion is allowed again.
	now = time.Unix(1900, 0).Add(6 * time.Minute)
	res2 := p.Promote(context.Background(), promoteProposal, promoteVerdict, cfg, testEvidence())
	if res2.Outcome == OutcomeDiscarded {
		t.Fatalf("post-cooldown outcome = %q, want NOT discarded (cooldown elapsed)", res2.Outcome)
	}
	if len(realDef.calls) != 1 {
		t.Errorf("SetRealDefault calls = %d, want 1 (apply allowed after cooldown elapsed)", len(realDef.calls))
	}
}

var errTelemetry = errTelemetryT("telemetry unreachable")

type errTelemetryT string

func (e errTelemetryT) Error() string { return string(e) }

// --- RE-WATCH scheduling (#1065 item 4) ---

// stepWatcher returns a sequence of verdicts across successive Observe calls — the
// stand-in for a window that fills with more real games over time.
type stepWatcher struct {
	mu       sync.Mutex
	verdicts []WatchVerdict
	i        int
}

func (s *stepWatcher) Observe(context.Context, string, float64, time.Time) (WatchVerdict, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := s.verdicts[len(s.verdicts)-1]
	if s.i < len(s.verdicts) {
		v = s.verdicts[s.i]
		s.i++
	}
	return v, nil
}

// Degraded satisfies the Watcher interface (collapse to bool).
func (s *stepWatcher) Degraded(ctx context.Context, f string, b float64, since time.Time) (bool, error) {
	v, err := s.Observe(ctx, f, b, since)
	return v == VerdictDegraded, err
}

// syncReverter is a thread-safe reverter for the background-re-watch tests (the
// revert runs on the poller goroutine while the test goroutine reads calls).
type syncReverter struct {
	mu    sync.Mutex
	calls int
}

func (r *syncReverter) RevertRealDefault(context.Context, string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls++
	return nil
}

func (r *syncReverter) Calls() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.calls
}

// TestInsufficientGamesSchedulesRewatchThatReverts: an apply whose initial watch is
// INSUFFICIENT schedules a re-watch; when the re-watch later confirms degradation it
// reverts. The promotion call itself returns Applied (it never blocks on real games).
func TestInsufficientGamesSchedulesRewatchThatReverts(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &syncReverter{}
	// Initial Observe: insufficient. Re-watch poll: insufficient again, then degraded.
	watcher := &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient, VerdictInsufficient, VerdictDegraded}}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, watcher, rev)

	rw := NewPollingRewatcher(context.Background(), time.Millisecond, time.Second, discardLogger())
	p.SetRewatcher(rw)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())

	// The promotion applied (insufficient evidence does not revert synchronously).
	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied (insufficient → apply stands, re-watch scheduled)", res.Outcome)
	}

	// The background re-watch eventually confirms degradation and reverts.
	waitFor(t, time.Second, func() bool { return rev.Calls() >= 1 })
	if rev.Calls() != 1 {
		t.Errorf("re-watch revert calls = %d, want 1", rev.Calls())
	}
}

// TestRewatchKeepsOnConfirmedHealthy: an insufficient apply whose re-watch later
// confirms HEALTHY keeps the apply (no revert).
func TestRewatchKeepsOnConfirmedHealthy(t *testing.T) {
	realDef := &recordingRealDefault{}
	rev := &syncReverter{}
	watcher := &stepWatcher{verdicts: []WatchVerdict{VerdictInsufficient, VerdictHealthy}}
	p, _ := promoterWithRecorder(t, &recordingGitHub{}, &recordingGit{}, realDef, watcher, rev)
	rw := NewPollingRewatcher(context.Background(), time.Millisecond, time.Second, discardLogger())
	p.SetRewatcher(rw)

	res := p.Promote(context.Background(), promoteProposal, promoteVerdict,
		Config{Mode: ModeAutonomous, Target: TargetLocal, Enabled: true}, testEvidence())
	if res.Outcome != OutcomeApplied {
		t.Fatalf("outcome = %q, want applied", res.Outcome)
	}
	// Give the re-watch a moment to observe HEALTHY and stop; it must NOT revert.
	time.Sleep(50 * time.Millisecond)
	if rev.Calls() != 0 {
		t.Errorf("re-watch revert calls = %d, want 0 (confirmed healthy)", rev.Calls())
	}
}

// countingObserver reports a fixed verdict and counts Observe calls (safe across
// the re-watch goroutine).
type countingObserver struct {
	verdict atomic.Int32
	calls   atomic.Int32
}

func (o *countingObserver) Observe(context.Context, string, float64, time.Time) (WatchVerdict, error) {
	o.calls.Add(1)
	return WatchVerdict(o.verdict.Load()), nil
}

func (o *countingObserver) Degraded(ctx context.Context, f string, b float64, since time.Time) (bool, error) {
	v, err := o.Observe(ctx, f, b, since)
	return v == VerdictDegraded, err
}

// TestRewatchStopsAtTimeout: with too few real games the re-watch polls every
// interval until the timeout, then stops and the default stays: a later
// degradation is no longer acted on.
// UC: UC-043 main
func TestRewatchStopsAtTimeout(t *testing.T) {
	t.Run("UC-043/main step 7 the re-watch timeout defaults to 3600 s", func(t *testing.T) {
		if DefaultRewatchTimeout != time.Hour {
			t.Fatalf("DefaultRewatchTimeout = %v, want 1h", DefaultRewatchTimeout)
		}
		if rw := NewPollingRewatcher(context.Background(), 0, 0, discardLogger()); rw.timeout() != DefaultRewatchTimeout || rw.interval() != DefaultRewatchInterval {
			t.Fatalf("unset interval/timeout = %v/%v, want the defaults", rw.interval(), rw.timeout())
		}
	})

	t.Run("UC-043/main step 7 after the timeout the re-watch stops and the default stays", func(t *testing.T) {
		var clock atomic.Int64
		base := time.Unix(1_700_000_000, 0)
		clock.Store(base.UnixNano())
		rw := NewPollingRewatcher(context.Background(), time.Millisecond, time.Minute, discardLogger())
		rw.now = func() time.Time { return time.Unix(0, clock.Load()) }

		obs := &countingObserver{}
		obs.verdict.Store(int32(VerdictInsufficient))
		var reverts atomic.Int32
		rw.Schedule(context.Background(), "f", 0.5, base, obs, func(context.Context) { reverts.Add(1) })

		// Before the timeout it keeps polling.
		waitFor(t, 5*time.Second, func() bool { return obs.calls.Load() >= 3 })

		// Past the timeout: the next insufficient poll ends the re-watch.
		clock.Store(base.Add(time.Minute + time.Second).UnixNano())
		stable := func() bool {
			before := obs.calls.Load()
			time.Sleep(30 * time.Millisecond) // ~30 polls at the 1 ms interval
			return obs.calls.Load() == before
		}
		waitFor(t, 5*time.Second, stable)

		// A degradation now is not observed: the re-watch is over, nothing reverts.
		obs.verdict.Store(int32(VerdictDegraded))
		settled := obs.calls.Load()
		time.Sleep(50 * time.Millisecond)
		if got := obs.calls.Load(); got != settled {
			t.Fatalf("re-watch polled %d more times after the timeout, want it stopped", got-settled)
		}
		if reverts.Load() != 0 {
			t.Fatalf("reverts = %d after the timeout, want 0 (the default stays)", reverts.Load())
		}
	})
}

// waitFor polls cond until it is true or the deadline passes (a test helper for the
// background re-watch, which acts on its own goroutine).
func waitFor(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("condition not met within %s", d)
}
