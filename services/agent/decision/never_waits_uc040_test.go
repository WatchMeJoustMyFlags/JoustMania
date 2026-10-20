package decision

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// never_waits_uc040_test.go: the failure postcondition's first clause (UC-040
// Post-F-1, BR-001): a remote decide call that hangs and then fails never holds
// up the decision cycle, so nothing in the game waits for the agent.

// UC: UC-040 Post-F-1 BR-001
func TestRemote_HangingFailedCallNeverHoldsTheCycle(t *testing.T) {
	t.Run("UC-040/Post-F-1 a hanging remote call that fails does not hold up OnEvaluate", func(t *testing.T) {
		be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
			return decide.Response{}, errors.New("upstream 500")
		})
		be.block = true
		h := newRemoteHarness(t, remoteSnapshot(time.Hour), be)

		done := make(chan struct{})
		go func() {
			h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(2 * time.Second):
			close(be.release)
			t.Fatal("OnEvaluate waited for the remote call (BR-001: the game never waits)")
		}
		select {
		case <-be.started:
		case <-time.After(2 * time.Second):
			t.Fatal("the remote call never started")
		}
		close(be.release) // the call now fails, after the cycle has returned
		h.loop.AwaitInflight()
		if be.callCount() != 1 {
			t.Fatalf("remote calls = %d, want 1", be.callCount())
		}
	})
}
