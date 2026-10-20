package decision

import (
	"context"
	"errors"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// decide_hook_talk_test.go drives the beats of the talk demo (UC-070) through the
// decide hook on the backend each beat uses: the threshold beat (step 3) and the
// degrade beat (step 7, A2) on Jev, the kill switch (step 5), the rollback from
// Claude to Jev (step 6) and the permissions beat on rules (A7).

// talkJevSnapshot is the stage baseline on the main in-game backend: Jev at its
// gate threshold (0.6), Claude at the 5 s default timeout.
func talkJevSnapshot(jevTimeout time.Duration) flags.Snapshot {
	s := defaultThresholdSnapshot(flags.DecisionBackendJev, jevTimeout)
	s.Decision.Claude.Timeout = 5 * time.Second
	return s
}

// talkCycle fires one cycle, waits for the remote call to land and returns the
// newest agent.decide span.
func talkCycle(t *testing.T, h remoteHarness) sdktrace.ReadOnlySpan {
	t.Helper()
	h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
	h.loop.AwaitInflight()
	ds := spansByName(h.sr.Ended(), SpanDecide)
	if len(ds) == 0 {
		t.Fatal("no agent.decide span")
	}
	return ds[len(ds)-1]
}

// UC: UC-070 main
func TestTalk_ThresholdBeatOnJev(t *testing.T) {
	t.Run("UC-070/main step 3 jev-threshold strict holds the answer that acted", func(t *testing.T) {
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Jev.ActConfidenceThreshold = 0.95
		h := newRemoteHarness(t, snap, jevBackend(nearThresholdConfidence))
		span := h.run(t)
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeHeld {
			t.Fatalf("outcome = %q, want held (0.72 < jev strict 0.95)", g)
		}
		if got := h.sink.decisions(); len(got) != 0 {
			t.Fatalf("held answer applied: %+v", got)
		}
		assertThreshold(t, span, 0.95, flags.DecisionBackendJev)
	})

	t.Run("UC-070/main step 3 a calm answer stays noop under strict", func(t *testing.T) {
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Jev.ActConfidenceThreshold = 0.95
		be := newFakeDecideBackend(ffaAnswer("noop", 0.84, "none", 0, "calm play"))
		be.name = flags.DecisionBackendJev
		h := newRemoteHarness(t, snap, be)
		span := h.run(t)
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeNoop {
			t.Fatalf("outcome = %q, want noop", g)
		}
		if got := h.sink.decisions(); len(got) != 0 {
			t.Fatalf("noop applied: %+v", got)
		}
	})

	t.Run("UC-070/main step 3 jev-threshold gate restores the act", func(t *testing.T) {
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Jev.ActConfidenceThreshold = 0.95
		h := newRemoteHarness(t, snap, jevBackend(nearThresholdConfidence))
		if g := strAttr(t, talkCycle(t, h), AttrDecideOutcome); g != DecideOutcomeHeld {
			t.Fatalf("strict outcome = %q, want held", g)
		}
		h.flags.mutate(func(s *flags.Snapshot) {
			s.Decision.Jev.ActConfidenceThreshold = flags.DefaultDecisionJevActConfidenceThreshold
		})
		span := talkCycle(t, h)
		if g := strAttr(t, span, AttrDecideOutcome); g != DecideOutcomeAct {
			t.Fatalf("gate outcome = %q, want act (0.72 >= 0.6)", g)
		}
		if got := h.sink.decisions(); len(got) != 1 {
			t.Fatalf("applied = %+v, want one decision after the restore", got)
		}
		assertThreshold(t, span, flags.DefaultDecisionJevActConfidenceThreshold, flags.DecisionBackendJev)
	})
}

// UC: UC-070 main
func TestTalk_KillSwitchBeat(t *testing.T) {
	t.Run("UC-070/main step 5 an in-flight claude answer is discarded with kill_switch", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		snap := talkJevSnapshot(time.Hour)
		snap.Decision.Backend = flags.DecisionBackendClaude
		h, span := fireAndMutateSnapshot(t, be, snap, time.Hour, func(h remoteHarness) {
			h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = false })
		})
		assertDiscarded(t, h, span, DecideDiscardKillSwitch)
		if g := strAttr(t, span, AttrDecideBackend); g != flags.DecisionBackendClaude {
			t.Fatalf("decide.backend = %q, want claude", g)
		}
	})

	for _, backend := range []string{flags.DecisionBackendRules, flags.DecisionBackendClaude, flags.DecisionBackendJev} {
		t.Run("UC-070/main step 5 kill switch off: no decision on "+backend, func(t *testing.T) {
			snap := talkJevSnapshot(time.Second)
			snap.Decision.Backend = backend
			snap.Enabled = false
			var be *fakeDecideBackend
			var reg decide.Backend
			if backend != flags.DecisionBackendRules {
				be = newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
				be.name = backend
				reg = be
			}
			h := newRemoteHarness(t, snap, reg)
			for i := 0; i < 3; i++ {
				state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
				h.loop.AwaitInflight()
				if len(state.Outcomes) != 0 {
					t.Fatalf("cycle %d decided %+v with the kill switch off", i, state.Outcomes)
				}
				advance(h.clock, 2*time.Second)
			}
			if be != nil && be.callCount() != 0 {
				t.Fatalf("backend calls = %d, want 0 with the kill switch off", be.callCount())
			}
			if got := h.sink.decisions(); len(got) != 0 {
				t.Fatalf("applied %+v with the kill switch off", got)
			}
			if n := len(spansByName(h.sr.Ended(), SpanDecision)); n != 0 {
				t.Fatalf("agent.decision spans = %d, want 0", n)
			}

			// The presenter switches the agent back on: the next cycle decides again.
			h.flags.mutate(func(s *flags.Snapshot) { s.Enabled = true })
			h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			if be != nil {
				h.loop.AwaitInflight() // rules decide synchronously
			}
			if got := h.sink.decisions(); len(got) == 0 {
				t.Fatal("nothing applied after switching back on, want the next cycle to decide")
			}
		})
	}
}

// UC: UC-070 main
func TestTalk_RollbackToJev(t *testing.T) {
	t.Run("UC-070/main step 6 agent-backend jev: the next decision is jev's, no restart", func(t *testing.T) {
		claude := newFakeDecideBackend(ffaAnswer("grant_shield", 0.9, "A", 0.5, "claude"))
		jev := jevBackend(nearThresholdConfidence)
		snap := talkJevSnapshot(time.Second)
		snap.Decision.Backend = flags.DecisionBackendClaude
		h := newRemoteHarness(t, snap, claude)
		reg := NewDecideRegistry()
		reg.Register(claude.Name(), func(flags.DecisionBackend) decide.Backend { return claude })
		reg.Register(jev.Name(), func(flags.DecisionBackend) decide.Backend { return jev })
		h.loop.SetDecideRegistry(reg)

		first := talkCycle(t, h)
		if g := strAttr(t, first, AttrDecideBackend); g != flags.DecisionBackendClaude {
			t.Fatalf("first decide.backend = %q, want claude", g)
		}
		h.flags.mutate(func(s *flags.Snapshot) { s.Decision.Backend = flags.DecisionBackendJev })
		second := talkCycle(t, h)
		for key, want := range map[string]string{
			AttrDecideBackend:          flags.DecisionBackendJev,
			AttrDecideBackendRequested: flags.DecisionBackendJev,
			AttrDecideOutcome:          DecideOutcomeAct,
		} {
			if g := strAttr(t, second, key); g != want {
				t.Errorf("after the rollback %s = %q, want %q", key, g, want)
			}
		}
		assertThreshold(t, second, flags.DefaultDecisionJevActConfidenceThreshold, flags.DecisionBackendJev)
		if claude.callCount() != 1 || jev.callCount() != 1 {
			t.Fatalf("calls claude=%d jev=%d, want 1 and 1", claude.callCount(), jev.callCount())
		}
	})
}

// NFR: NFR-010 NFR-013
// UC: UC-070 main A2 Post-F-1
// UC: UC-040 A2 A3
func TestTalk_DegradeBeatOnJev(t *testing.T) {
	t.Run("UC-070/main step 7 blackhole base_url: the jev timeout falls back to rules in the same cycle", func(t *testing.T) {
		be := jevBackend(nearThresholdConfidence)
		be.block = true
		h := newRemoteHarness(t, talkJevSnapshot(20*time.Millisecond), be)
		start := time.Now()
		span := h.run(t)
		// The 2.5 s Jev timeout applies, not Claude's 5 s.
		if elapsed := time.Since(start); elapsed > 2*time.Second {
			t.Fatalf("fallback landed after %v, want the jev timeout (20ms)", elapsed)
		}
		assertRulesFallbackFrom(t, h, span, flags.DecisionBackendJev, DecideFallbackTimeout, DecideFallbackTimeout)
	})

	t.Run("UC-070/A2 jev rejected by the network: error falls back to rules", func(t *testing.T) {
		be := newFakeDecideBackend(func(decide.Request) (decide.Response, error) {
			return decide.Response{}, errors.New("dial tcp 192.0.2.1:443: connect: network is unreachable")
		})
		be.name = flags.DecisionBackendJev
		h := newRemoteHarness(t, talkJevSnapshot(time.Second), be)
		span := h.run(t)
		assertRulesFallbackFrom(t, h, span, flags.DecisionBackendJev, DecideFallbackError, "*errors.errorString")
	})

	t.Run("UC-070/A2 jev without its key: backend_unavailable, rules decide synchronously", func(t *testing.T) {
		be := jevBackend(nearThresholdConfidence)
		be.available = false
		h := newRemoteHarness(t, talkJevSnapshot(time.Second), be)
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		if len(state.Outcomes) == 0 {
			t.Fatal("rules must decide in the same cycle when jev is unavailable")
		}
		if be.callCount() != 0 {
			t.Fatalf("jev calls = %d, want 0 when unavailable", be.callCount())
		}
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		for key, want := range map[string]string{
			AttrDecideBackend:          "rules",
			AttrDecideBackendRequested: flags.DecisionBackendJev,
			AttrDecideFallbackReason:   DecideFallbackUnavailable,
			AttrDecideOutcome:          DecideOutcomeFallback,
		} {
			if g := strAttr(t, ds[0], key); g != want {
				t.Errorf("%s = %q, want %q", key, g, want)
			}
		}
		if got := h.sink.decisions(); len(got) == 0 || got[0].Intervention != InterventionAdjustMusicTempo {
			t.Fatalf("applied %+v, want the rules decision adjust_music_tempo (the game keeps playing)", got)
		}
	})
}

// UC: UC-070 A7
func TestTalk_PermissionsBeatOnRules(t *testing.T) {
	t.Run("UC-070/A7 permissions none on rules: every non-noop decision is blocked not_allowed", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.InterventionsAllowed = nil // the `none` variant: the empty string on the wire
		h := newRemoteHarness(t, snap, nil)
		state := h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
		if len(state.Outcomes) == 0 {
			t.Fatal("rules must still decide under none (and be blocked), not skip the cycle")
		}
		for i, o := range state.Outcomes {
			if o.Dispatched || o.BlockReason != ReasonNotAllowed {
				t.Errorf("outcome %d dispatched=%v reason=%q, want blocked not_allowed", i, o.Dispatched, o.BlockReason)
			}
		}
		if got := h.sink.decisions(); len(got) != 0 {
			t.Fatalf("applied %+v under none", got)
		}
		dec := spansByName(h.sr.Ended(), SpanDecision)
		if len(dec) == 0 {
			t.Fatal("no agent.decision span: the blocked decision must be visible in Jaeger")
		}
		for _, sp := range dec {
			if g := strAttr(t, sp, AttrDecisionBlockReason); g != string(ReasonNotAllowed) {
				t.Errorf("decision.block_reason = %q, want not_allowed", g)
			}
		}
	})
}
