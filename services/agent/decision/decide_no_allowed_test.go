package decision

import (
	"bytes"
	"context"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// decide_no_allowed_test.go covers the empty-allow-list call gate (#1298): with
// nothing allowed, a remote decide call could only answer noop, so none is made.

// syncBuffer is a goroutine-safe log sink.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) count(s string) int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return strings.Count(b.buf.String(), s)
}

// noAllowedHarness is a remote harness whose gated counter and debug log are
// observable.
func noAllowedHarness(t *testing.T, allowed []string, be *fakeDecideBackend) (remoteHarness, *metric.ManualReader, *syncBuffer) {
	t.Helper()
	snap := remoteSnapshot(time.Second)
	snap.InterventionsAllowed = allowed
	h := newRemoteHarness(t, snap, be)
	reader := metric.NewManualReader()
	h.loop.llmGated = newLLMGatedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
	logs := &syncBuffer{}
	h.loop.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
	return h, reader, logs
}

const budgetCapForTest = 100 // remoteSnapshot's llm.max_requests_per_minute

// UC: UC-040 A14 BR-005
func TestRemote_NoInterventionsAllowed(t *testing.T) {
	empties := map[string][]string{
		"none (nil)":        nil,
		"none (empty list)": {},
		"empty id":          {""},
		"probe (noop only)": {"noop"},
		"noop and empties":  {"noop", "", "noop"},
	}
	for name, allowed := range empties {
		t.Run("UC-040/A14 "+name+" makes no remote call and charges nothing", func(t *testing.T) {
			be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
			h, reader, logs := noAllowedHarness(t, allowed, be)
			const cycles = 3
			for i := 0; i < cycles; i++ {
				state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
				if len(state.Outcomes) != 0 {
					t.Fatalf("cycle %d decided %+v, want nothing (rules must not run either)", i, state.Outcomes)
				}
				advance(h.clock, 2*time.Second)
			}
			h.loop.AwaitInflight()

			if n := be.callCount(); n != 0 {
				t.Fatalf("backend calls = %d, want 0", n)
			}
			if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != 0 {
				t.Fatalf("agent.decide spans = %d, want 0 (no span per skipped cycle, not a fallback)", n)
			}
			if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
				t.Fatalf("agent.decision spans = %d, want 0 (rules must not run)", n)
			}
			if got := h.sink.decisions(); len(got) != 0 {
				t.Fatalf("applied %+v, want nothing", got)
			}
			if !h.loop.lastDecideAttempt.IsZero() {
				t.Fatal("cadence charged: lastDecideAttempt set although no call was made")
			}
			if left := slotsLeft(h.loop.budget, h.loop.nowOrDefault()(), budgetCapForTest); left != budgetCapForTest {
				t.Fatalf("budget slots left = %d, want %d (no slot charged)", left, budgetCapForTest)
			}
			gated := gatedByReason(t, reader)
			if gated[DecideGateNoInterventions] != cycles {
				t.Fatalf("gated[%s] = %d, want %d (all: %v)", DecideGateNoInterventions, gated[DecideGateNoInterventions], cycles, gated)
			}
			if n := logs.count("agent.decide.no_interventions_allowed"); n != 1 {
				t.Fatalf("state-change debug logs = %d, want 1 over %d skipped cycles", n, cycles)
			}
		})
	}

	t.Run("UC-040/A14 one allowed intervention still calls the backend", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		h, reader, _ := noAllowedHarness(t, []string{"noop", "grant_shield"}, be)
		h.run(t)
		if n := be.callCount(); n != 1 {
			t.Fatalf("backend calls = %d, want 1", n)
		}
		if h.loop.lastDecideAttempt.IsZero() {
			t.Fatal("an admitted call must charge the cadence")
		}
		if left := slotsLeft(h.loop.budget, h.loop.nowOrDefault()(), budgetCapForTest); left != budgetCapForTest-1 {
			t.Fatalf("budget slots left = %d, want %d", left, budgetCapForTest-1)
		}
		if g := gatedByReason(t, reader)[DecideGateNoInterventions]; g != 0 {
			t.Fatalf("gated[%s] = %d, want 0", DecideGateNoInterventions, g)
		}
	})

	t.Run("UC-040/A14 calls resume when the allow-list is restored", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		h, _, logs := noAllowedHarness(t, nil, be)
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.flags.mutate(func(s *flags.Snapshot) { s.InterventionsAllowed = []string{"grant_shield"} })
		h.run(t)
		if n := be.callCount(); n != 1 {
			t.Fatalf("backend calls = %d, want 1 after the allow-list was restored", n)
		}
		// One line entering the state, one leaving it.
		if n := logs.count("agent.decide.no_interventions_allowed"); n != 2 {
			t.Fatalf("state-change debug logs = %d, want 2", n)
		}
	})

	t.Run("UC-040/A14 ineligible game kind still falls back to rules first", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("noop", 0.9, "none", 0, ""))
		h, reader, _ := noAllowedHarness(t, nil, be)
		c := remoteContext()
		c.GameKind = "shadow"
		h.loop.OnEvaluate(context.Background(), c, testTrigger())
		gated := gatedByReason(t, reader)
		if gated[FallbackNotEligible] != 1 || gated[DecideGateNoInterventions] != 0 {
			t.Fatalf("gated = %v, want only %s", gated, FallbackNotEligible)
		}
		if be.callCount() != 0 {
			t.Fatal("shadow game must not call the backend")
		}
	})
}

// TestHasAllowedInterventions_MatchesFFAOptions: the gate's notion of "empty" is
// exactly "ffa.intervention would offer noop only".
//
// UC: UC-040 BR-005
func TestHasAllowedInterventions_MatchesFFAOptions(t *testing.T) {
	cases := map[string][]string{
		"nil":                    nil,
		"empty list":             {},
		"empty id":               {""},
		"noop only":              {"noop"},
		"noop and empty id":      {"noop", ""},
		"one intervention":       {"grant_shield"},
		"mixed with noop":        {"", "noop", "play_audio_cue"},
		"duplicate intervention": {"x", "x"},
	}
	for name, allowed := range cases {
		t.Run("UC-040/BR-005 "+name, func(t *testing.T) {
			qs := decide.FFAQuestions(decide.State{}, allowed)
			wantNonEmpty := len(qs[0].Options) > 1
			if got := hasAllowedInterventions(allowed); got != wantNonEmpty {
				t.Fatalf("hasAllowedInterventions(%q) = %v, but ffa.intervention options = %v", allowed, got, qs[0].Options)
			}
		})
	}
}

// TestRemote_NoInterventionsAllowedAtLanding: a call fired while an intervention
// was allowed lands after interventions_allowed flipped to none. The landing
// re-reads the live allow-list, so the answer is blocked (not_allowed), not applied.
//
// UC: UC-040 A10 A14 BR-005
func TestRemote_NoInterventionsAllowedAtLanding(t *testing.T) {
	t.Run("UC-040/A14 flip to none mid-flight blocks the landing answer", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h, span := fireAndMutate(t, be, time.Hour, func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.InterventionsAllowed = nil })
		})
		if got := h.sink.decisions(); len(got) != 0 {
			t.Fatalf("applied %+v although interventions_allowed flipped to none mid-flight", got)
		}
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
			t.Fatalf("outcome = %q, want act (blocked by the permission chain)", g)
		}
		dec := spansByName(h.sr.Ended(), SpanDecision)
		if len(dec) != 1 || strAttr(t, dec[0], AttrDecisionBlockReason) != string(ReasonNotAllowed) {
			t.Fatal("agent.decision must be blocked with not_allowed by the LIVE allow-list")
		}
		// The next cycle makes no new call.
		advance(h.clock, time.Minute)
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		if n := be.callCount(); n != 1 {
			t.Fatalf("backend calls = %d, want 1 (no new call after the flip)", n)
		}
	})
}
