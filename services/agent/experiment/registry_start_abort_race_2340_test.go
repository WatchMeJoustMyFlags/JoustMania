package experiment

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/joustmania/agent/experiment/journal"
)

// registry_start_abort_race_2340_test.go (#2340): a Start whose targeting write
// (TargetingWriter.Apply, outside r.mu) lands after a teardown's strip (AbortAll:
// kill switch, experiments_enabled off, shutdown) must strip it again, so the kill
// switch leaves no agent targeting behind (UC-045 step 5).

// liveTargeting models the flag service: Apply writes the experiment's branch,
// Strip removes it. onApply runs before the write lands (the in-flight window);
// onStrip runs before a strip, after it is counted.
type liveTargeting struct {
	mu       sync.Mutex
	live     map[string]bool
	strips   int
	stripErr error
	onApply  func(n int)
	onStrip  func(n int)
	applies  int
}

func newLiveTargeting() *liveTargeting { return &liveTargeting{live: map[string]bool{}} }

func (l *liveTargeting) Apply(ctx context.Context, p Proposal) error {
	l.mu.Lock()
	l.applies++
	n, hook := l.applies, l.onApply
	l.mu.Unlock()
	if hook != nil {
		hook(n)
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.live[p.ExperimentID] = true
	return nil
}

func (l *liveTargeting) Strip(ctx context.Context, _ string, id string) error {
	l.mu.Lock()
	l.strips++
	n, hook := l.strips, l.onStrip
	l.mu.Unlock()
	if hook != nil {
		hook(n)
	}
	if err := ctx.Err(); err != nil {
		return err // a cancelled context strips nothing
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stripErr != nil {
		return l.stripErr
	}
	delete(l.live, id)
	return nil
}

func (l *liveTargeting) isLive(id string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.live[id]
}

func (l *liveTargeting) stripCount() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.strips
}

// abortAllConcurrently runs AbortAll on another goroutine and waits for it, so the
// race detector sees the teardown and the Start on different goroutines.
func abortAllConcurrently(r *Registry, ctx context.Context, reason string) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		r.AbortAll(ctx, reason)
	}()
	<-done
}

// UC: UC-045 main
func TestUC045_StartRacingAbortAllLeavesNoTargeting_2340(t *testing.T) {
	t.Run("UC-045/main an AbortAll that strips before the Start's write lands leaves no targeting", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		target.onApply = func(int) { abortAllConcurrently(r, context.Background(), "kill-switch: agent disabled") }

		if err := r.Start(context.Background(), id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if target.isLive(id) {
			t.Fatal("the experiment's targeting branch survived the kill switch")
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Fatalf("status = %s, want %s", st, StatusAborted)
		}
	})

	t.Run("UC-045/main a write that lands while the teardown strip runs is stripped and never RUNNING", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		stripping, release := make(chan struct{}), make(chan struct{})
		target.onStrip = func(n int) {
			if n == 1 { // the teardown's strip: hold it until the Start returned
				close(stripping)
				<-release
			}
		}
		target.onApply = func(int) { <-stripping } // the write lands after the teardown began
		aborted := make(chan struct{})
		go func() {
			defer close(aborted)
			r.AbortAll(context.Background(), "experiments_enabled flag off")
		}()

		if err := r.Start(context.Background(), id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if st, _ := r.Status(id); st == StatusRunning {
			t.Fatal("a Start whose write landed during the teardown made the experiment RUNNING")
		}
		close(release)
		<-aborted
		if target.isLive(id) {
			t.Fatal("the experiment's targeting branch survived the teardown")
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Fatalf("status = %s, want %s", st, StatusAborted)
		}
	})

	t.Run("UC-045/main a Start that lost to a concurrent Start keeps the live targeting", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		target.onApply = func(n int) {
			if n == 1 {
				if err := r.Start(context.Background(), id); err != nil {
					t.Errorf("inner Start: %v", err)
				}
			}
		}
		if err := r.Start(context.Background(), id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if st, _ := r.Status(id); st != StatusRunning {
			t.Fatalf("status = %s, want %s", st, StatusRunning)
		}
		if !target.isLive(id) || target.stripCount() != 0 {
			t.Fatalf("live=%v strips=%d, want the RUNNING experiment's targeting kept", target.isLive(id), target.stripCount())
		}
	})

	t.Run("UC-045/main a failed strip of the late write is journaled and returned", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		target.onApply = func(int) {
			abortAllConcurrently(r, context.Background(), "kill-switch: agent disabled")
			target.mu.Lock()
			target.stripErr = errors.New("flag service unreachable")
			target.mu.Unlock()
		}

		err = r.Start(context.Background(), id)
		if !errors.Is(err, ErrStartTargetingLeaked) {
			t.Fatalf("Start err = %v, want ErrStartTargetingLeaked", err)
		}
		if got := target.stripCount(); got != 1+abortedStartStripAttempts {
			t.Fatalf("strips = %d, want the teardown's plus %d attempts", got, abortedStartStripAttempts)
		}
		var noted bool
		for _, ev := range eventsOfKind(journalEvents(t, r, id), journal.KindDecision) {
			if strings.HasPrefix(ev.Note, noteStartStripFailed) && ev.Status == "" {
				noted = true
			}
		}
		if !noted {
			t.Fatalf("no %q decision journaled", noteStartStripFailed)
		}
	})
}

// UC: UC-043 A20
func TestUC043_A20_StartRacingShutdownLeavesNoTargeting_2340(t *testing.T) {
	t.Run("UC-043/A20 the shutdown cancels the Start's context; its late write is still stripped", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		startCtx, cancel := context.WithCancel(context.Background())
		defer cancel()
		target.onApply = func(int) {
			abortAllConcurrently(r, context.Background(), "agent shutdown")
		}
		// The write lands, then the stop cancels the Start's context before the re-fetch.
		applyThenCancel := &cancelAfterApply{liveTargeting: target, cancel: cancel}
		r.target = applyThenCancel

		if err := r.Start(startCtx, id); err != nil {
			t.Fatalf("Start: %v", err)
		}
		if target.isLive(id) {
			t.Fatal("the experiment's targeting branch survived the shutdown")
		}
	})
}

// UC: UC-045 main
func TestUC045_StartApplyErrorAfterWriteLeavesNoTargeting_2340(t *testing.T) {
	t.Run("UC-045/main an Apply that wrote the branch and then failed is still stripped after a teardown", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		startCtx, cancel := context.WithCancel(context.Background())
		defer cancel()
		target.onApply = func(int) { abortAllConcurrently(r, context.Background(), "agent shutdown") }
		r.target = &writeThenFail{liveTargeting: target, cancel: cancel}

		err = r.Start(startCtx, id)
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("Start err = %v, want the Apply error (context.Canceled)", err)
		}
		if target.isLive(id) {
			t.Fatal("the branch an erroring Apply wrote after the teardown's strip survived")
		}
		if st, _ := r.Status(id); st != StatusAborted {
			t.Fatalf("status = %s, want %s", st, StatusAborted)
		}
	})

	t.Run("UC-045/main an Apply error with no teardown strips nothing and leaves the experiment PROPOSED", func(t *testing.T) {
		target := newLiveTargeting()
		r := newTestRegistry(t, RegistryConfig{MaxShadowGames: 4, Targeting: target})
		id, err := r.Declare(sampleIntent())
		if err != nil {
			t.Fatalf("Declare: %v", err)
		}
		startCtx, cancel := context.WithCancel(context.Background())
		defer cancel()
		r.target = &writeThenFail{liveTargeting: target, cancel: cancel}

		if err := r.Start(startCtx, id); !errors.Is(err, context.Canceled) {
			t.Fatalf("Start err = %v, want context.Canceled", err)
		}
		if got := target.stripCount(); got != 0 {
			t.Fatalf("strips = %d, want 0 (the caller's abort owns the strip)", got)
		}
		if st, _ := r.Status(id); st != StatusProposed {
			t.Fatalf("status = %s, want %s", st, StatusProposed)
		}
	})
}

// writeThenFail writes the branch on the flag service, then the caller's context is
// cancelled and Apply returns ctx.Err(): the request reached the server, the client
// saw an error (the 500 ms cap or the shutdown).
type writeThenFail struct {
	*liveTargeting
	cancel context.CancelFunc
}

func (w *writeThenFail) Apply(ctx context.Context, p Proposal) error {
	if err := w.liveTargeting.Apply(context.WithoutCancel(ctx), p); err != nil {
		return err
	}
	w.cancel()
	return ctx.Err()
}

// cancelAfterApply cancels the Start's context once its write has landed, as the
// graceful stop does while the Start is between its write and its re-fetch.
type cancelAfterApply struct {
	*liveTargeting
	cancel context.CancelFunc
}

func (c *cancelAfterApply) Apply(ctx context.Context, p Proposal) error {
	err := c.liveTargeting.Apply(ctx, p)
	c.cancel()
	return err
}
