package decision

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/gamecontext"
)

// chargeledger_uc040_test.go covers the agent's shield-charge ledger (UC-040 A31 step 1,
// BR-019, BR-020; decide-contract §4.5 step 3; #1837 PR-4a): in trigger mode the agent
// gives a player at most one shield charge per window, blocks a second grant_shield
// already_charged before the rate limit, keeps an entry only for an applied write, and
// stays inert outside a real Free-for-All game in trigger mode.

// triggerMode is a shield-mode source that reads trigger with the given expiry and
// counts its calls.
func triggerMode(expiry time.Duration, calls *atomic.Int32) ShieldChargeMode {
	return func(context.Context, string) (bool, time.Duration) {
		if calls != nil {
			calls.Add(1)
		}
		return true, expiry
	}
}

func instantMode(context.Context, string) (bool, time.Duration) {
	return false, 20 * time.Second
}

// ffaRealCtx is an active real Free-for-All game with players WEAK and S.
func ffaRealCtx() gamecontext.GameContext {
	c := budgetCtx()
	c.GameKind = gamecontext.GameKindReal
	mode := shieldTriggerGameMode
	c.Session.GameMode = &mode
	return c
}

// ledgerLoop is budgetLoop with the shield-mode source set.
func ledgerLoop(t *testing.T, budget int, mode ShieldChargeMode, out ...Decision) (*Loop, *capturingSink, *time.Time, func() int) {
	t.Helper()
	l, sink, clock, spans := budgetLoop(t, budget, out...)
	l.SetShieldChargeMode(mode)
	return l, sink, clock, spans
}

func runCycle(l *Loop, c gamecontext.GameContext) LayerState {
	return l.OnEvaluate(context.Background(), c, testTrigger())
}

// UC: UC-040 A31 BR-020
func TestChargeLedger_SecondGrantBlockedNoBudget(t *testing.T) {
	t.Run("UC-040/A31 a second grant within the window is already_charged and charges no budget", func(t *testing.T) {
		shieldCost := int(interventionCost(InterventionGrantShield))
		l, sink, clock, _ := ledgerLoop(t, 2*shieldCost, triggerMode(20*time.Second, nil), budgetShield("WEAK"))

		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("first grant outcomes = %v, want dispatched", got)
		}
		*clock = clock.Add(5 * time.Second)
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:already_charged"}) {
			t.Fatalf("second grant outcomes = %v, want already_charged", got)
		}
		// The refusal drew no budget: one more shield (another player) still fits.
		l.Rules = fakeRules{out: []Decision{budgetShield("S")}}
		*clock = clock.Add(time.Second)
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/S:dispatched"}) {
			t.Fatalf("grant to S outcomes = %v, want dispatched (the refusal must not charge the budget)", got)
		}
		if n := len(sink.decisions()); n != 2 {
			t.Fatalf("writes = %d, want 2 (WEAK once, S once)", n)
		}
	})
}

// UC: UC-040 A31 BR-019
func TestChargeLedger_Inert(t *testing.T) {
	werewolf, unknown := "Werewolf", ""
	for _, tc := range []struct {
		name      string
		mode      func(*atomic.Int32) ShieldChargeMode
		ctx       func() gamecontext.GameContext
		wantReads int32
	}{
		{"UC-040/A31 instant mode", func(*atomic.Int32) ShieldChargeMode { return instantMode }, ffaRealCtx, -1},
		{"UC-040/A31 nil mode source", func(*atomic.Int32) ShieldChargeMode { return nil }, ffaRealCtx, -1},
		{"UC-040/A31 unknown game mode", func(c *atomic.Int32) ShieldChargeMode { return triggerMode(20*time.Second, c) },
			func() gamecontext.GameContext { c := ffaRealCtx(); c.Session.GameMode = nil; return c }, 0},
		{"UC-040/A31 empty game mode", func(c *atomic.Int32) ShieldChargeMode { return triggerMode(20*time.Second, c) },
			func() gamecontext.GameContext { c := ffaRealCtx(); c.Session.GameMode = &unknown; return c }, 0},
		{"UC-040/A31 BR-019 a non-FFA mode", func(c *atomic.Int32) ShieldChargeMode { return triggerMode(20*time.Second, c) },
			func() gamecontext.GameContext { c := ffaRealCtx(); c.Session.GameMode = &werewolf; return c }, 0},
		{"UC-040/A31 BR-019 a shadow game", func(c *atomic.Int32) ShieldChargeMode { return triggerMode(20*time.Second, c) },
			func() gamecontext.GameContext { c := ffaRealCtx(); c.GameKind = gamecontext.GameKindShadow; return c }, 0},
		{"UC-040/A31 an unlabelled game kind", func(c *atomic.Int32) ShieldChargeMode { return triggerMode(20*time.Second, c) },
			func() gamecontext.GameContext { c := ffaRealCtx(); c.GameKind = ""; return c }, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var reads atomic.Int32
			l, sink, clock, _ := ledgerLoop(t, 100, tc.mode(&reads), budgetShield("WEAK"))
			for i := 0; i < 2; i++ {
				if got := outcomeSummary(runCycle(l, tc.ctx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
					t.Fatalf("grant %d outcomes = %v, want dispatched (the coordinator decides)", i, got)
				}
				*clock = clock.Add(5 * time.Second)
			}
			if n := len(sink.decisions()); n != 2 {
				t.Fatalf("writes = %d, want 2", n)
			}
			if tc.wantReads >= 0 && reads.Load() != tc.wantReads {
				t.Fatalf("shield-mode reads = %d, want %d (the flag is read only after the cheap checks)", reads.Load(), tc.wantReads)
			}
		})
	}

	t.Run("UC-040/A31 other interventions and a session-scoped grant are never read or blocked", func(t *testing.T) {
		var reads atomic.Int32
		session := Decision{Intervention: InterventionGrantShield, ObjectiveServed: ObjectiveBalanced, Objectives: balancedFocused()}
		l, _, clock, _ := ledgerLoop(t, 100, triggerMode(20*time.Second, &reads), budgetNudge("WEAK"), session)
		for i := 0; i < 2; i++ {
			for _, o := range runCycle(l, ffaRealCtx()).Outcomes {
				if !o.Dispatched {
					t.Fatalf("cycle %d: %s/%s blocked %s, want dispatched", i, o.Intervention, o.TargetSerial, o.BlockReason)
				}
			}
			*clock = clock.Add(5 * time.Second)
		}
		if reads.Load() != 0 {
			t.Fatalf("shield-mode reads = %d, want 0", reads.Load())
		}
	})
}

// UC: UC-040 A31
func TestChargeLedger_Window(t *testing.T) {
	for _, tc := range []struct {
		name   string
		expiry time.Duration
		window time.Duration
	}{
		{"UC-040/A31 expiry 10 s gives 12 s", 10 * time.Second, 12 * time.Second},
		{"UC-040/A31 expiry 20 s gives 22 s", 20 * time.Second, 22 * time.Second},
		{"UC-040/A31 expiry 0 (never) gives 22 s", 0, 22 * time.Second},
		{"UC-040/A31 expiry 45 s is capped at 22 s", 45 * time.Second, 22 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := ledgerLoop(t, 100, triggerMode(tc.expiry, nil), budgetShield("WEAK"))
			start := *clock
			_ = runCycle(l, ffaRealCtx())
			*clock = start.Add(tc.window - time.Millisecond)
			if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:already_charged"}) {
				t.Fatalf("at window-1ms outcomes = %v, want already_charged", got)
			}
			*clock = start.Add(tc.window)
			if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("at the window end outcomes = %v, want dispatched", got)
			}
		})
	}
}

// UC: UC-040 A31 A11 A27
func TestChargeLedger_ReleasedWhenNotWritten(t *testing.T) {
	for _, tc := range []struct {
		name string
		sink ActionSink
	}{
		{"UC-040/A31 UC-040/A11 a gated-off write keeps no entry", &gatedSink{gateOn: false}},
		{"UC-040/A31 UC-040/A27 a failed write keeps no entry", errSink{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := ledgerLoop(t, 100, triggerMode(20*time.Second, nil), budgetShield("WEAK"))
			l.Actions = tc.sink
			_ = runCycle(l, ffaRealCtx())
			*clock = clock.Add(time.Second)
			l.Actions = &capturingSink{}
			if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("next grant outcomes = %v, want dispatched (nothing was written)", got)
			}
		})
	}

	t.Run("UC-040/A31 a rate-limit block after the reservation keeps no entry", func(t *testing.T) {
		l, _, clock, _ := ledgerLoop(t, 2, triggerMode(20*time.Second, nil), budgetShield("WEAK"))
		l.limiter.allow(clock.Add(-rateWindow+500*time.Millisecond), 2, 2) // full now, frees in 0.5 s
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:rate_limit"}) {
			t.Fatalf("first grant outcomes = %v, want rate_limit", got)
		}
		*clock = clock.Add(time.Second)
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("next grant outcomes = %v, want dispatched (the rate limit released the reservation)", got)
		}
	})

	t.Run("UC-040/A31 a stale reservation handle neither commits nor releases a newer entry", func(t *testing.T) {
		var cl chargeLedger
		now := time.Unix(1_000, 0)
		old, _, _ := cl.reserve("WEAK", now, chargeOnly(22*time.Second))
		old.now = func() time.Time { return now }
		cl.release("WEAK", old.gen)
		fresh, _, _ := cl.reserve("WEAK", now, chargeOnly(22*time.Second))
		if fresh == nil {
			t.Fatal("reserve after release refused")
		}
		old.finish(nil) // stale release
		if res, _, _ := cl.reserve("WEAK", now.Add(time.Second), chargeOnly(22*time.Second)); res != nil {
			t.Fatal("a stale release dropped the newer entry")
		}
		applied := true
		old.finish(&applied) // stale commit
		if e := cl.entries["WEAK"]; e.chargeCommitted {
			t.Fatal("a stale commit committed the newer entry")
		}
	})
}

// UC: UC-040 A31
func TestChargeLedger_ConcurrentReserve(t *testing.T) {
	t.Run("UC-040/A31 concurrent decisions for one player dispatch exactly one grant", func(t *testing.T) {
		l, sink, clock, _ := ledgerLoop(t, 100, triggerMode(20*time.Second, nil))
		snap := rulesSnapshot()
		c := ffaRealCtx()
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := 0; i < 16; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				var state LayerState // each goroutine its own cycle state
				<-start
				l.runDecision(context.Background(), snap, c, budgetShield("WEAK"), *clock, &state)
			}()
		}
		close(start)
		wg.Wait()
		if n := len(sink.decisions()); n != 1 {
			t.Fatalf("writes = %d, want exactly 1", n)
		}
	})
}

// UC: UC-040 A31 A21
func TestChargeLedger_RepeatRefusalQuiet(t *testing.T) {
	t.Run("UC-040/A31 only the first refusal per entry is emitted; repeats only count", func(t *testing.T) {
		l, _, clock, decisionSpans := ledgerLoop(t, 100, triggerMode(20*time.Second, nil), budgetShield("WEAK"))
		reader := metric.NewManualReader()
		l.decisions = newDecisionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		logs := &syncBuffer{}
		l.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
		var blockedTimeline atomic.Int32
		l.SetInterventionRecorder(func(_, _ string, blocked bool) {
			if blocked {
				blockedTimeline.Add(1)
			}
		})

		_ = runCycle(l, ffaRealCtx()) // the grant
		const refusals = 3
		for i := 0; i < refusals; i++ {
			*clock = clock.Add(2 * time.Second)
			st := runCycle(l, ffaRealCtx())
			if got := outcomeSummary(st); !equalStrings(got, []string{"grant_shield/WEAK:already_charged"}) {
				t.Fatalf("refusal %d outcomes = %v, want already_charged in the layer state", i, got)
			}
		}
		if n := decisionSpans(); n != 2 {
			t.Fatalf("agent.decision spans = %d, want 2 (the grant and the first refusal)", n)
		}
		if n := logs.count("agent.decision_blocked"); n != 1 {
			t.Errorf("agent.decision_blocked warnings = %d, want 1", n)
		}
		if n := logs.count("level=DEBUG msg=agent.decision_already_charged_repeat"); n != refusals-1 {
			t.Errorf("repeat debug lines = %d, want %d", n, refusals-1)
		}
		if n := blockedTimeline.Load(); n != 1 {
			t.Errorf("blocked timeline entries = %d, want 1", n)
		}
		if got := decisionsByLabels(t, reader)["grant_shield/true/already_charged"]; got != refusals {
			t.Errorf("agent_decisions_total{grant_shield,true,already_charged} = %d, want %d", got, refusals)
		}
	})
}

// UC: UC-040 A31
func TestChargeLedger_NotCountedAsIntervention(t *testing.T) {
	t.Run("UC-040/A31 a refusal is not dispatched, applied or written", func(t *testing.T) {
		l, sink, clock, _ := ledgerLoop(t, 100, triggerMode(20*time.Second, nil), budgetShield("WEAK"))
		reader := metric.NewManualReader()
		l.interventionsApplied = newInterventionsAppliedCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		var mu sync.Mutex
		var timeline []bool
		var outcomes []gamecontext.ApplyOutcome
		l.SetInterventionRecorder(func(_, _ string, blocked bool) {
			mu.Lock()
			defer mu.Unlock()
			timeline = append(timeline, blocked)
		})
		l.SetApplyOutcomeRecorder(func(_ string, o gamecontext.ApplyOutcome) {
			mu.Lock()
			defer mu.Unlock()
			outcomes = append(outcomes, o)
		})

		_ = runCycle(l, ffaRealCtx())
		*clock = clock.Add(5 * time.Second)
		_ = runCycle(l, ffaRealCtx())

		mu.Lock()
		defer mu.Unlock()
		if len(timeline) != 2 || timeline[0] || !timeline[1] {
			t.Fatalf("timeline = %v, want [dispatched, blocked]", timeline)
		}
		if len(outcomes) != 1 || outcomes[0] != gamecontext.ApplyOutcomeApplied {
			t.Fatalf("apply outcomes = %v, want only the first grant applied", outcomes)
		}
		if n := len(sink.decisions()); n != 1 {
			t.Fatalf("writes = %d, want 1", n)
		}
		if got := appliedByAction(t, reader)[InterventionGrantShield]; got != 1 {
			t.Fatalf("agent_interventions_applied_total{grant_shield} = %d, want 1", got)
		}
	})
}

// UC: UC-040 A31
func TestChargeLedger_DecideSpanReportsAlreadyCharged(t *testing.T) {
	t.Run("UC-040/A31 a remote answer landing for a charged player is blocked already_charged", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		h := newRemoteHarness(t, remoteSnapshot(time.Second), be)
		h.loop.SetShieldChargeMode(triggerMode(20*time.Second, nil))
		c := remoteContext()
		mode := shieldTriggerGameMode
		c.Session.GameMode = &mode
		h.provider.set(remoteGameID, c)
		// The agent already wrote a charge for A in this game.
		res, _, _ := h.loop.charges.reserve("A", h.loop.nowOrDefault()(), chargeOnly(chargeLedgerWindow(20*time.Second)))
		res.now = h.loop.nowOrDefault()
		applied := true
		res.finish(&applied)

		_ = h.loop.OnEvaluate(context.Background(), c, testTrigger())
		h.loop.AwaitInflight()
		ds := spansByName(h.sr.Ended(), SpanDecide)
		if len(ds) != 1 {
			t.Fatalf("agent.decide spans = %d, want 1", len(ds))
		}
		if v, ok := attrValue(ds[0], AttrDecideBlocked); !ok || !v.AsBool() {
			t.Fatal("decide.blocked must be true")
		}
		if g := strAttr(t, ds[0], AttrDecideBlockReason); g != string(ReasonAlreadyCharged) {
			t.Fatalf("decide.block_reason = %q, want already_charged", g)
		}
		if n := len(h.sink.decisions()); n != 0 {
			t.Fatalf("writes = %d, want 0", n)
		}
	})
}
