package experiment

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// registry_reassert_abort_race_2362_test.go (#2362): the arm-value re-assert before
// a pair (UC-043 A10, TargetingWriter.Reassert, outside r.mu) whose write lands
// after a teardown's strip (Abort / AbortAll: kill switch, experiments_enabled off,
// shutdown) must strip it again, so the kill switch leaves no agent targeting
// behind (UC-045 step 5). Same class as #2340 (Start).

// reassertLiveTargeting is liveTargeting with the Reassert capability: onReassert
// runs before the write lands (the in-flight window); failAfterWrite makes the
// re-assert return an error after its write reached the flag service.
type reassertLiveTargeting struct {
	*liveTargeting
	rmu            sync.Mutex
	reasserts      int
	onReassert     func(n int)
	failAfterWrite error
}

func newReassertLiveTargeting() *reassertLiveTargeting {
	return &reassertLiveTargeting{liveTargeting: newLiveTargeting()}
}

func (f *reassertLiveTargeting) Reassert(ctx context.Context, p Proposal) error {
	f.rmu.Lock()
	f.reasserts++
	n, hook, failErr := f.reasserts, f.onReassert, f.failAfterWrite
	f.rmu.Unlock()
	if hook != nil {
		hook(n)
	}
	f.liveTargeting.mu.Lock()
	f.liveTargeting.live[p.ExperimentID] = true
	f.liveTargeting.mu.Unlock()
	return failErr
}

func (f *reassertLiveTargeting) reassertCount() int {
	f.rmu.Lock()
	defer f.rmu.Unlock()
	return f.reasserts
}

// startedReassertRegistry declares and starts one experiment with a live
// targeting writer that can re-assert, and a spawner.
func startedReassertRegistry(t *testing.T) (*Registry, *reassertLiveTargeting, *fakeSpawner, string) {
	t.Helper()
	target := newReassertLiveTargeting()
	spawner := &fakeSpawner{}
	r := newTestRegistry(t, RegistryConfig{
		MaxShadowGames: 2,
		Spawner:        spawner,
		Targeting:      target,
		Verdict:        fakeVerdict{concludeAtN: 1000},
	})
	id, err := r.Declare(sampleIntent())
	if err != nil {
		t.Fatalf("Declare: %v", err)
	}
	if err := r.Start(context.Background(), id); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if !target.isLive(id) {
		t.Fatal("Start wrote no targeting")
	}
	return r, target, spawner, id
}

// abortConcurrently runs Abort on another goroutine and waits for it.
func abortConcurrently(t *testing.T, r *Registry, id, reason string) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := r.Abort(context.Background(), id, reason); err != nil {
			t.Errorf("Abort: %v", err)
		}
	}()
	<-done
}

// UC: UC-045 main
func TestUC045_ReassertRacingAbortLeavesNoTargeting_2362(t *testing.T) {
	t.Run("UC-045/main an Abort that strips before the re-assert's write lands leaves no targeting", func(t *testing.T) {
		r, target, spawner, id := startedReassertRegistry(t)
		target.onReassert = func(int) { abortConcurrently(t, r, id, "kill-switch: agent disabled") }

		if n := r.AllocateAndSpawn(context.Background()); n != 0 {
			t.Fatalf("spawned %d games for an aborted experiment, want 0", n)
		}
		if target.isLive(id) {
			t.Fatal("the targeting branch the re-assert wrote after the abort survived")
		}
		if got := spawner.attemptCount(); got != 0 {
			t.Fatalf("spawn attempts = %d, want none", got)
		}
		if got := r.TotalInFlight(); got != 0 {
			t.Fatalf("in flight = %d, want the pair's reservations released", got)
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Fatalf("status = %s, want %s", st, StatusAborted)
		}
	})

	t.Run("UC-045/main a re-assert that wrote and then failed after an AbortAll is still stripped", func(t *testing.T) {
		r, target, _, id := startedReassertRegistry(t)
		target.failAfterWrite = context.DeadlineExceeded // the client's request cap
		target.onReassert = func(int) { abortAllConcurrently(r, context.Background(), "experiments_enabled flag off") }

		if n := r.AllocateAndSpawn(context.Background()); n != 0 {
			t.Fatalf("spawned %d games, want 0", n)
		}
		if target.isLive(id) {
			t.Fatal("the branch an erroring re-assert wrote after the teardown's strip survived")
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Fatalf("status = %s, want %s", st, StatusAborted)
		}
	})

	t.Run("UC-045/main a shutdown that cancels the tick's context still strips the late write", func(t *testing.T) {
		r, target, _, id := startedReassertRegistry(t)
		tickCtx, cancel := context.WithCancel(context.Background())
		defer cancel()
		target.onReassert = func(int) {
			abortAllConcurrently(r, context.Background(), "agent shutdown")
			cancel()
		}

		r.AllocateAndSpawn(tickCtx)
		if target.isLive(id) {
			t.Fatal("the re-asserted branch survived the shutdown")
		}
	})

	t.Run("UC-045/main a teardown that began before the re-assert writes nothing", func(t *testing.T) {
		r, target, spawner, id := startedReassertRegistry(t)
		stripping, release := make(chan struct{}), make(chan struct{})
		target.onStrip = func(n int) {
			if n == 1 { // the teardown's strip: hold it until the tick returned
				close(stripping)
				<-release
			}
		}
		aborted := make(chan struct{})
		go func() {
			defer close(aborted)
			r.AbortAll(context.Background(), "kill-switch: agent disabled")
		}()
		<-stripping

		if n := r.AllocateAndSpawn(context.Background()); n != 0 {
			t.Fatalf("spawned %d games during the teardown, want 0", n)
		}
		if got := target.reassertCount(); got != 0 {
			t.Fatalf("re-asserts = %d, want none once the teardown began", got)
		}
		if got := spawner.attemptCount(); got != 0 {
			t.Fatalf("spawn attempts = %d, want none", got)
		}
		close(release)
		<-aborted
		if target.isLive(id) {
			t.Fatal("the targeting branch survived the teardown")
		}
	})

	t.Run("UC-045/main a failed strip of the late re-assert is journaled", func(t *testing.T) {
		r, target, _, id := startedReassertRegistry(t)
		target.onReassert = func(int) {
			abortConcurrently(t, r, id, "kill-switch: agent disabled")
			target.liveTargeting.mu.Lock()
			target.stripErr = errors.New("flag service unreachable")
			target.liveTargeting.mu.Unlock()
		}

		r.AllocateAndSpawn(context.Background())
		if got := target.stripCount(); got != 1+abortedStartStripAttempts {
			t.Fatalf("strips = %d, want the teardown's plus %d attempts", got, abortedStartStripAttempts)
		}
		var noted bool
		for _, ev := range eventsOfKind(journalEvents(t, r, id), journal.KindDecision) {
			if strings.HasPrefix(ev.Note, noteReassertStripFailed) && ev.Status == "" {
				noted = true
			}
		}
		if !noted {
			t.Fatalf("no %q decision journaled", noteReassertStripFailed)
		}
	})
}

// UC: UC-043 A10
func TestUC043_A10_ReassertWithoutTeardownKeepsTargeting_2362(t *testing.T) {
	t.Run("UC-043/A10 a normal re-assert keeps its targeting and spawns the pair", func(t *testing.T) {
		r, target, spawner, id := startedReassertRegistry(t)

		if n := r.AllocateAndSpawn(context.Background()); n != 2 {
			t.Fatalf("spawned %d games, want the pair (2)", n)
		}
		if got := target.reassertCount(); got != 1 {
			t.Fatalf("re-asserts = %d, want 1", got)
		}
		if !target.isLive(id) || target.stripCount() != 0 {
			t.Fatalf("live=%v strips=%d, want the RUNNING experiment's targeting kept", target.isLive(id), target.stripCount())
		}
		if got := spawner.count(); got != 2 {
			t.Fatalf("spawned games = %d, want 2", got)
		}
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("status = %s, want %s", st, StatusRunning)
		}
	})

	t.Run("UC-043/A10 a failed re-assert with no teardown strips nothing and stays running", func(t *testing.T) {
		r, target, _, id := startedReassertRegistry(t)
		target.failAfterWrite = errors.New("flag service down (test)")

		if n := r.AllocateAndSpawn(context.Background()); n != 0 {
			t.Fatalf("spawned %d games, want 0", n)
		}
		if got := target.stripCount(); got != 0 {
			t.Fatalf("strips = %d, want 0", got)
		}
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("status = %s, want %s", st, StatusRunning)
		}
	})
}
