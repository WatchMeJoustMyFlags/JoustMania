package decision

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

// retro_backend_source_test.go covers inference.backend for the retro (#1810,
// C-035): the backend is resolved once per retro, a flip changes the NEXT retro, and
// a stub retro never charges the shared budget.

// UC: UC-042 Pre-2
func TestRetroBackendSource_ResolvedOncePerRetro_FlipAppliesToNext(t *testing.T) {
	claude := &countingText{}
	var resolves atomic.Int32
	var useClaude atomic.Bool
	useClaude.Store(true)

	rc, sr, budget, now := budgetedRetro(t, 1, nil)
	rc.SetBackendSource(func(context.Context) TextBackend {
		resolves.Add(1)
		if useClaude.Load() {
			return claude
		}
		return nil // the stub
	})

	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()
	if got := resolves.Load(); got != 1 {
		t.Fatalf("source resolved %d times for one retro, want exactly 1", got)
	}
	if claude.count() != 1 {
		t.Fatalf("claude calls = %d, want 1", claude.count())
	}
	if left := slotsLeft(budget, now, 1); left != 0 {
		t.Fatalf("a claude retro left %d budget slots, want it charged", left)
	}

	// Flip to the stub: the next retro (new session) is capture-only and charges nothing.
	useClaude.Store(false)
	other := endedSession()
	other.SessionID = "session-8"
	rc.OnGameEnd(other)
	rc.AwaitInflight()
	if claude.count() != 1 {
		t.Fatalf("claude calls = %d after the flip to stub, want still 1", claude.count())
	}
	if left := slotsLeft(budget, now.Add(time.Hour), 1); left != 1 {
		t.Fatalf("a stub retro after the flip charged the budget (free slots %d, want 1)", left)
	}
	if n := len(spansByName(sr.Ended(), SpanLLMRetro)); n != 2 {
		t.Fatalf("retro spans = %d, want 2", n)
	}
}

// The source overrides a fixed SetBackend; a nil result is the stub even when a fixed
// backend was set before.
// UC: UC-042 Pre-2
func TestRetroBackendSource_NilIsStubEvenWithFixedBackend(t *testing.T) {
	fixed := &countingText{}
	rc, _, _, _ := budgetedRetro(t, 6, fixed)
	rc.SetBackendSource(func(context.Context) TextBackend { return nil })
	rc.OnGameEnd(endedSession())
	rc.AwaitInflight()
	if fixed.count() != 0 {
		t.Fatalf("fixed backend called %d times, want 0 (the source said stub)", fixed.count())
	}
}
