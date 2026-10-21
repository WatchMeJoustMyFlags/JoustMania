package decision

import (
	"context"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// probe_select_test.go covers the rulesFor seam (#1810, C-035): the
// decision.probe_decisions flag swaps the probe engine in per cycle, never in a
// pattern game.

type recordingSink struct {
	mu  sync.Mutex
	got []string
}

func (s *recordingSink) Apply(_ context.Context, d Decision) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.got = append(s.got, d.Intervention)
	return nil
}

func (s *recordingSink) last() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.got) == 0 {
		return ""
	}
	return s.got[len(s.got)-1]
}

func probeLoop(t *testing.T) (*Loop, *settableFlags, *recordingSink) {
	t.Helper()
	l, _, fl := newTestLoop(t)
	sink := &recordingSink{}
	l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "real"}}}
	clock := time.Unix(1000, 0)
	// The clock steps past the probe interval on every read, so the probe is
	// never rate-limited here (its limiter has its own test).
	l.ProbeRules = NewProbeRules(time.Second, func() time.Time { clock = clock.Add(time.Hour); return clock })
	l.Actions = sink
	return l, fl, sink
}

// UC: UC-040 A23
func TestRulesFor_ProbeFlagPerCycle(t *testing.T) {
	ctx := context.Background()

	t.Run("UC-040/A23 flag off keeps the real engine", func(t *testing.T) {
		l, _, sink := probeLoop(t)
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if sink.last() != "grant_shield" {
			t.Fatalf("applied %q, want the real engine's grant_shield", sink.last())
		}
	})

	t.Run("UC-040/A23 a mid-game flip changes the NEXT cycle's engine", func(t *testing.T) {
		l, fl, sink := probeLoop(t)
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if sink.last() != "grant_shield" {
			t.Fatalf("cycle 1 applied %q, want grant_shield", sink.last())
		}
		fl.snap.ProbeDecisions = true
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if sink.last() != InterventionNoop {
			t.Fatalf("cycle 2 applied %q, want the probe's noop after the flip", sink.last())
		}
		fl.snap.ProbeDecisions = false
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if sink.last() != "grant_shield" {
			t.Fatalf("cycle 3 applied %q, want the real engine after the flip back", sink.last())
		}
	})

	t.Run("UC-040/A23 pattern games ignore the flag (UC-046 exactness)", func(t *testing.T) {
		l, fl, sink := probeLoop(t)
		l.SetPatternObserver(&fakePatternObserver{active: true})
		fl.snap.ProbeDecisions = true
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if sink.last() != "grant_shield" {
			t.Fatalf("applied %q, want the real engine in a pattern game", sink.last())
		}
	})

	t.Run("UC-040/A23 the probe keeps its rate-limit state across flips", func(t *testing.T) {
		l, fl, sink := probeLoop(t)
		clock := time.Unix(1000, 0)
		l.ProbeRules = NewProbeRules(5*time.Second, func() time.Time { return clock })
		fl.snap.ProbeDecisions = true
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		fl.snap.ProbeDecisions = false
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		fl.snap.ProbeDecisions = true
		before := len(sink.got)
		l.OnEvaluate(ctx, gamecontext.GameContext{}, testTrigger())
		if len(sink.got) != before {
			t.Fatalf("probe fired again inside its interval after a flip: %v", sink.got)
		}
	})
}
