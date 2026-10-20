package decision

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// decide_hook_talk_flows_test.go drives the talk demo (UC-070) beats that
// decide_hook_talk_test.go leaves open: the switch to Jev (step 2), Jev down at
// the venue (A2), Claude timing out (A3), the second vendor (A6), the answer
// that lands before the kill switch (A10), and every beat's observable in one
// run (Post-S-1).

// scriptedBackend is a decide.Backend whose answer the test swaps between beats.
// A blocking answer waits for the call context or release.
type scriptedBackend struct {
	name string

	mu        sync.Mutex
	available bool
	answer    func(context.Context, decide.Request) (decide.Response, error)
	calls     int
	started   chan struct{} // signalled once per call
}

func newScriptedBackend(name string, answer func(decide.Request) (decide.Response, error)) *scriptedBackend {
	b := &scriptedBackend{name: name, available: true, started: make(chan struct{}, 16)}
	b.set(answer)
	return b
}

func (b *scriptedBackend) Name() string { return b.name }

func (b *scriptedBackend) Available() bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.available
}

func (b *scriptedBackend) set(answer func(decide.Request) (decide.Response, error)) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.answer = func(_ context.Context, req decide.Request) (decide.Response, error) { return answer(req) }
}

// blockUntil makes the next calls wait for release (or the call's timeout)
// before answering with answer.
func (b *scriptedBackend) blockUntil(release <-chan struct{}, answer func(decide.Request) (decide.Response, error)) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.answer = func(ctx context.Context, req decide.Request) (decide.Response, error) {
		select {
		case <-ctx.Done():
			return decide.Response{}, ctx.Err()
		case <-release:
		}
		return answer(req)
	}
}

func (b *scriptedBackend) Decide(ctx context.Context, req decide.Request) (decide.Response, error) {
	b.mu.Lock()
	b.calls++
	answer := b.answer
	b.mu.Unlock()
	b.started <- struct{}{}
	return answer(ctx, req)
}

func (b *scriptedBackend) callCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.calls
}

// talkHarness is a remote harness whose registry serves the given backends.
func talkHarness(t *testing.T, snap flags.Snapshot, backends ...decide.Backend) remoteHarness {
	t.Helper()
	h := newRemoteHarness(t, snap, nil)
	reg := NewDecideRegistry()
	for _, be := range backends {
		reg.Register(be.Name(), func(flags.DecisionBackend) decide.Backend { return be })
	}
	h.loop.SetDecideRegistry(reg)
	return h
}

// nextTalkCycle advances past any cadence and fires one cycle (talkCycle).
func nextTalkCycle(t *testing.T, h remoteHarness) sdktrace.ReadOnlySpan {
	t.Helper()
	advance(h.clock, time.Minute)
	return talkCycle(t, h)
}

func assertSpanAttrs(t *testing.T, beat string, span sdktrace.ReadOnlySpan, want map[string]string) {
	t.Helper()
	for key, w := range want {
		if g := strAttr(t, span, key); g != w {
			t.Errorf("%s: %s = %q, want %q", beat, key, g, w)
		}
	}
}

var (
	calmNoop   = ffaAnswer("noop", 0.84, "none", 0, "calm play")
	actShield  = ffaAnswer("grant_shield", nearThresholdConfidence, "A", 0.5, "near threshold")
	claudeAct  = ffaAnswer("grant_shield", 0.9, "A", 0.5, "claude")
	networkErr = func(decide.Request) (decide.Response, error) {
		return decide.Response{}, errors.New("dial tcp 192.0.2.1:443: connect: network is unreachable")
	}
)

// UC: UC-070 main
func TestTalk_SwitchToJevBeat(t *testing.T) {
	t.Run("UC-070/main step 2 agent-backend jev: calm play is noop, a near-death game acts", func(t *testing.T) {
		jev := newScriptedBackend(flags.DecisionBackendJev, calmNoop)
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendRules
		h := talkHarness(t, snap, jev)

		first := nextTalkCycle(t, h)
		assertSpanAttrs(t, "rules (step 1)", first, map[string]string{AttrDecideBackend: "rules"})
		applied := len(h.sink.decisions())

		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendJev })
		calm := nextTalkCycle(t, h)
		assertSpanAttrs(t, "calm play on jev", calm, map[string]string{
			AttrDecideBackend: flags.DecisionBackendJev, AttrDecideOutcome: DecideOutcomeNoop,
		})
		if got := len(h.sink.decisions()); got != applied {
			t.Fatalf("calm noop applied a decision: %d -> %d", applied, got)
		}

		jev.set(actShield)
		act := nextTalkCycle(t, h)
		assertSpanAttrs(t, "near-death play on jev", act, map[string]string{
			AttrDecideBackend: flags.DecisionBackendJev, AttrDecideOutcome: DecideOutcomeAct,
		})
		assertThreshold(t, act, flags.DefaultDecisionJevActConfidenceThreshold, flags.DecisionBackendJev)
		got := h.sink.decisions()
		if len(got) != applied+1 || got[len(got)-1].Intervention != "grant_shield" || got[len(got)-1].TargetSerial != "A" {
			t.Fatalf("applied %+v, want the jev grant_shield on A (0.72 >= gate 0.6)", got)
		}
		if jev.callCount() != 2 {
			t.Fatalf("jev calls = %d, want 2", jev.callCount())
		}
	})
}

// UC: UC-070 A2
func TestTalk_JevDownThresholdBeatOnClaude(t *testing.T) {
	t.Run("UC-070/A2 jev down: rules decide, then the threshold beat runs on claude's global threshold", func(t *testing.T) {
		jev := newScriptedBackend(flags.DecisionBackendJev, networkErr)
		claude := newScriptedBackend(flags.DecisionBackendClaude, claudeAct)
		h := talkHarness(t, talkJevSnapshot(time.Second), jev, claude)

		down := nextTalkCycle(t, h)
		assertSpanAttrs(t, "jev down", down, map[string]string{
			AttrDecideBackend: "rules", AttrDecideBackendRequested: flags.DecisionBackendJev,
			AttrDecideFallbackReason: DecideFallbackError,
		})
		rulesApplied := len(h.sink.decisions())
		if got := h.sink.decisions(); rulesApplied == 0 || got[0].Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("applied %+v, want the rules decisions (the game did not stall)", got)
		}

		// A2 step 2: the presenter flips the backup backend.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendClaude })
		// A2 step 3: threshold strict (global) holds the answer that acted.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.ActConfidenceThreshold = 0.95 })
		held := nextTalkCycle(t, h)
		assertSpanAttrs(t, "claude strict", held, map[string]string{
			AttrDecideBackend: flags.DecisionBackendClaude, AttrDecideOutcome: DecideOutcomeHeld,
		})
		assertThreshold(t, held, 0.95, flags.ActThresholdSourceGlobal)

		// threshold default restores it.
		h.flags.mutate(func(s *flags.Snapshot) {
			s.Decision.ActConfidenceThreshold = flags.DefaultDecisionActConfidenceThreshold
		})
		act := nextTalkCycle(t, h)
		assertSpanAttrs(t, "claude default", act, map[string]string{
			AttrDecideBackend: flags.DecisionBackendClaude, AttrDecideOutcome: DecideOutcomeAct,
		})
		assertThreshold(t, act, flags.DefaultDecisionActConfidenceThreshold, flags.ActThresholdSourceGlobal)
		if got := h.sink.decisions(); len(got) != rulesApplied+1 || got[rulesApplied].Intervention != "grant_shield" {
			t.Fatalf("applied %+v, want claude's grant_shield after the restore", got)
		}
	})
}

// UC: UC-070 A3
func TestTalk_ClaudeTimeoutPatientBeat(t *testing.T) {
	claudeSnap := func(timeout time.Duration) flags.Snapshot {
		s := talkJevSnapshot(time.Second)
		s.Decision.Backend = flags.DecisionBackendClaude
		s.Decision.Claude.Timeout = timeout
		return s
	}

	t.Run("UC-070/A3 a claude timeout falls back; the patient timeout lets the next call land", func(t *testing.T) {
		release := make(chan struct{})
		claude := newScriptedBackend(flags.DecisionBackendClaude, claudeAct)
		claude.blockUntil(release, claudeAct)
		h := talkHarness(t, claudeSnap(20*time.Millisecond), claude)

		timedOut := nextTalkCycle(t, h)
		assertSpanAttrs(t, "default timeout", timedOut, map[string]string{
			AttrDecideBackend: "rules", AttrDecideBackendRequested: flags.DecisionBackendClaude,
			AttrDecideOutcome: DecideOutcomeFallback, AttrDecideFallbackReason: DecideFallbackTimeout,
		})

		// The presenter selects the patient variant (8000 ms); the call that takes
		// longer than the old timeout now lands.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Claude.Timeout = 8 * time.Second })
		advance(h.clock, time.Minute)
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		<-claude.started
		<-claude.started
		time.Sleep(60 * time.Millisecond) // well past the old 20 ms timeout
		close(release)
		h.loop.AwaitInflight()
		ds := spansByName(h.sr.Ended(), SpanDecide)
		assertSpanAttrs(t, "patient timeout", ds[len(ds)-1], map[string]string{
			AttrDecideBackend: flags.DecisionBackendClaude, AttrDecideOutcome: DecideOutcomeAct,
		})
	})

	t.Run("UC-070/A3 a call that still times out after the kill switch is discarded, not a fallback", func(t *testing.T) {
		be := newFakeDecideBackend(claudeAct)
		h, span := fireAndMutateSnapshot(t, be, claudeSnap(20*time.Millisecond), 20*time.Millisecond, func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
		})
		assertDiscarded(t, h, span, DecideDiscardKillSwitch)
	})
}

// UC: UC-070 A6
func TestTalk_SecondVendorBeat(t *testing.T) {
	t.Run("UC-070/A6 openai without a model or key: openai -> rules backend_unavailable, then back to jev", func(t *testing.T) {
		openai := newScriptedBackend(flags.DecisionBackendOpenAI, claudeAct)
		openai.available = false // shipped `unset` model or no DECISION_OPENAI_API_KEY
		jev := newScriptedBackend(flags.DecisionBackendJev, actShield)
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendOpenAI
		h := talkHarness(t, snap, openai, jev)

		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		if len(state.Outcomes) == 0 {
			t.Fatal("rules must decide in the same cycle when openai is unavailable")
		}
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		assertSpanAttrs(t, "openai unavailable", ds[0], map[string]string{
			AttrDecideBackend: "rules", AttrDecideBackendRequested: flags.DecisionBackendOpenAI,
			AttrDecideFallbackReason: DecideFallbackUnavailable, AttrDecideOutcome: DecideOutcomeFallback,
		})
		if openai.callCount() != 0 {
			t.Fatalf("openai calls = %d, want 0", openai.callCount())
		}

		// A6 step 2: agent-backend.sh jev.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendJev })
		back := nextTalkCycle(t, h)
		assertSpanAttrs(t, "back to jev", back, map[string]string{
			AttrDecideBackend: flags.DecisionBackendJev, AttrDecideOutcome: DecideOutcomeAct,
		})
	})
}

// UC: UC-070 A10
func TestTalk_AnswerLandsBeforeKillSwitch(t *testing.T) {
	t.Run("UC-070/A10 the landed answer keeps its outcome; no later cycle decides until on", func(t *testing.T) {
		claude := newScriptedBackend(flags.DecisionBackendClaude, claudeAct)
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendClaude
		h := talkHarness(t, snap, claude)

		landed := nextTalkCycle(t, h)
		assertSpanAttrs(t, "landed before the kill switch", landed, map[string]string{
			AttrDecideBackend: flags.DecisionBackendClaude, AttrDecideOutcome: DecideOutcomeAct,
		})
		if got := h.sink.decisions(); len(got) != 1 || got[0].Intervention != "grant_shield" {
			t.Fatalf("applied %+v, want the landed act applied", got)
		}

		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
		for i := 0; i < 3; i++ {
			advance(h.clock, time.Minute)
			h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			h.loop.AwaitInflight()
		}
		if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != 1 {
			t.Fatalf("agent.decide rows = %d while off, want only the landed one (no discarded row)", n)
		}
		if claude.callCount() != 1 || len(h.sink.decisions()) != 1 {
			t.Fatalf("calls=%d applied=%d while off, want 1 and 1", claude.callCount(), len(h.sink.decisions()))
		}

		// A10 step 3: agent-killswitch.sh on; the next cycle decides again.
		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = true })
		again := nextTalkCycle(t, h)
		assertSpanAttrs(t, "after on", again, map[string]string{AttrDecideOutcome: DecideOutcomeAct})
		if claude.callCount() != 2 {
			t.Fatalf("claude calls = %d after on, want 2", claude.callCount())
		}
	})
}

// UC: UC-070 Post-S-1
func TestTalk_EveryBeatShowsItsObservable(t *testing.T) {
	t.Run("UC-070/Post-S-1 steps 1-7 in one run, each with its agent.decide row", func(t *testing.T) {
		jev := newScriptedBackend(flags.DecisionBackendJev, actShield)
		claude := newScriptedBackend(flags.DecisionBackendClaude, claudeAct)
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendRules
		h := talkHarness(t, snap, jev, claude)
		setBackend := func(b string) { h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = b }) }

		// Step 1: a decide row on rules.
		assertSpanAttrs(t, "step 1", nextTalkCycle(t, h), map[string]string{AttrDecideBackend: "rules"})

		// Step 2: rows with backend jev, an act applied.
		setBackend(flags.DecisionBackendJev)
		assertSpanAttrs(t, "step 2", nextTalkCycle(t, h), map[string]string{
			AttrDecideBackend: flags.DecisionBackendJev, AttrDecideOutcome: DecideOutcomeAct,
		})

		// Step 3: held under the strict jev threshold, then the gate is restored.
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Jev.ActConfidenceThreshold = 0.95 })
		step3 := nextTalkCycle(t, h)
		assertSpanAttrs(t, "step 3", step3, map[string]string{AttrDecideOutcome: DecideOutcomeHeld})
		assertThreshold(t, step3, 0.95, flags.DecisionBackendJev)
		h.flags.mutate(func(s *flags.Snapshot) {
			s.Decision.Jev.ActConfidenceThreshold = flags.DefaultDecisionJevActConfidenceThreshold
		})

		// Step 4: rows with backend claude and the global threshold.
		setBackend(flags.DecisionBackendClaude)
		step4 := nextTalkCycle(t, h)
		assertSpanAttrs(t, "step 4", step4, map[string]string{AttrDecideBackend: flags.DecisionBackendClaude})
		assertThreshold(t, step4, snap.Decision.ActConfidenceThreshold, flags.ActThresholdSourceGlobal)

		// Step 5: a discarded row with kill_switch, then no new rows while off.
		release := make(chan struct{})
		claude.blockUntil(release, claudeAct)
		advance(h.clock, time.Minute)
		// The call is in flight (blocked until release) when the kill switch flips.
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
		close(release)
		h.loop.AwaitInflight()
		ds := spansByName(h.sr.Ended(), SpanDecide)
		assertSpanAttrs(t, "step 5", ds[len(ds)-1], map[string]string{
			AttrDecideOutcome: DecideOutcomeDiscarded, AttrDecideDiscardedReason: DecideDiscardKillSwitch,
		})
		rows := len(ds)
		advance(h.clock, time.Minute)
		h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		h.loop.AwaitInflight()
		if n := len(spansByName(h.sr.Ended(), SpanDecide)); n != rows {
			t.Fatalf("step 5: %d new rows while the agent is off, want 0", n-rows)
		}
		h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = true })

		// Step 6: rows with backend jev again (no restart: same loop).
		setBackend(flags.DecisionBackendJev)
		assertSpanAttrs(t, "step 6", nextTalkCycle(t, h), map[string]string{
			AttrDecideBackend: flags.DecisionBackendJev, AttrDecideOutcome: DecideOutcomeAct,
		})

		// Step 7: jev -> rules with error, and the game keeps playing.
		jev.set(networkErr)
		before := len(h.sink.decisions())
		assertSpanAttrs(t, "step 7", nextTalkCycle(t, h), map[string]string{
			AttrDecideBackend: "rules", AttrDecideBackendRequested: flags.DecisionBackendJev,
			AttrDecideFallbackReason: DecideFallbackError,
		})
		if got := h.sink.decisions(); len(got) <= before || got[before].Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("step 7 applied %+v, want the rules decisions", got[before:])
		}
	})
}
