package decision

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
)

// shieldlimit_uc040_test.go covers the agent's shield cap and cooldown per player per
// game (UC-040 A33, BR-022; decide-contract §4.5 step 3; #2044): the merged shield
// ledger refuses a grant_shield shield_cap_reached or shield_cooldown before the rate
// limit, releases every reservation that was not written, keeps a pending grant from
// passing the cooldown, and refuses nothing at 0/0.

// chargeOnly is a ledger request with only the trigger-mode charge window (no limits).
func chargeOnly(window time.Duration) shieldRequest {
	return shieldRequest{chargeWindow: window, seconds: DefaultShieldSeconds * time.Second}
}

// limitLoop is budgetLoop with the shield limits set in its snapshot.
func limitLoop(t *testing.T, budget, maxShields, cooldownSeconds int, out ...Decision) (*Loop, *capturingSink, *time.Time, func() int) {
	t.Helper()
	l, sink, clock, spans := budgetLoop(t, budget, out...)
	snap := l.Flags.Evaluate(context.Background())
	snap.Policy.MaxShieldsPerPlayerPerGame = maxShields
	snap.Policy.ShieldCooldownSeconds = cooldownSeconds
	l.Flags = fakeFlags{snap: snap}
	return l, sink, clock, spans
}

func limiterEntries(l *Loop) int {
	l.limiter.mu.Lock()
	defer l.limiter.mu.Unlock()
	return len(l.limiter.entries)
}

func shieldWritesTo(sink *capturingSink, serial string) int {
	n := 0
	for _, d := range sink.decisions() {
		if d.Intervention == InterventionGrantShield && d.TargetSerial == serial {
			n++
		}
	}
	return n
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_RulesAcceptance(t *testing.T) {
	t.Run("UC-040/A33 BR-022 rules at 50/min shield one near-threshold player exactly 3 times in 120 s", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Objectives = balancedFocused()
		snap.Policy = flags.Policy{MaxInterventionsPerMinute: 50, MaxShieldsPerPlayerPerGame: 3, ShieldCooldownSeconds: 15}
		l, _ := decideRecordingLoop(t, snap)
		clock := time.Unix(50_000, 0)
		l.now = func() time.Time { return clock }
		sink := &capturingSink{}
		l.Actions = sink
		rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
		rules.SetShieldSource(l.ShieldSerials)
		l.Rules = rules

		for elapsed := time.Duration(0); elapsed <= 120*time.Second; elapsed += 5 * time.Second {
			for _, o := range runCycle(l, chargeRulesCtx()).Outcomes {
				if o.Intervention == InterventionGrantShield && !o.Dispatched {
					t.Fatalf("at %v the rules proposed a refused shield (%s): the no-shield filter must drop it", elapsed, o.BlockReason)
				}
			}
			clock = clock.Add(5 * time.Second)
		}
		if n := shieldWritesTo(sink, "WEAK"); n != 3 {
			t.Fatalf("grant_shield writes to WEAK = %d, want exactly 3", n)
		}
		// Then the cap: a 4th grant is refused shield_cap_reached without charging
		// the limiter.
		before := limiterEntries(l)
		var st LayerState
		l.runDecision(context.Background(), snap, chargeRulesCtx(), budgetShield("WEAK"), clock, &st)
		if got := outcomeSummary(st); !equalStrings(got, []string{"grant_shield/WEAK:shield_cap_reached"}) {
			t.Fatalf("4th grant outcomes = %v, want shield_cap_reached", got)
		}
		if after := limiterEntries(l); after != before {
			t.Fatalf("limiter entries %d -> %d: a cap refusal must not charge the budget", before, after)
		}
		if _, noShield := l.ShieldSerials(); !noShield["WEAK"] {
			t.Fatal("WEAK must be in the rules' no-shield set at the cap")
		}
	})
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_RemoteAlwaysGrantsShield(t *testing.T) {
	t.Run("UC-040/A33 a remote backend that always answers grant_shield is capped at 3", func(t *testing.T) {
		be := newFakeDecideBackend(ffaAnswer("grant_shield", 0.95, "A", 0.5, "x"))
		snap := remoteSnapshot(time.Second)
		snap.Policy.MaxShieldsPerPlayerPerGame = 3
		snap.Policy.ShieldCooldownSeconds = 15
		h := newRemoteHarness(t, snap, be)
		h.loop.now = func() time.Time { return *h.clock }
		var last string
		for i := 0; i < 5; i++ {
			before := len(spansByName(h.sr.Ended(), SpanDecide))
			_ = h.loop.OnEvaluate(context.Background(), remoteContext(), testTrigger())
			h.loop.AwaitInflight()
			ds := spansByName(h.sr.Ended(), SpanDecide)
			if len(ds) != before+1 {
				t.Fatalf("cycle %d: agent.decide spans = %d, want %d", i, len(ds), before+1)
			}
			last = ""
			if v, ok := attrValue(ds[len(ds)-1], AttrDecideBlockReason); ok {
				last = v.AsString()
			}
			if i < 3 && last != "" {
				t.Fatalf("cycle %d: decide.block_reason = %q, want a write", i, last)
			}
			advance(h.clock, 30*time.Second) // past the cooldown (6.5 s + 2 s + 15 s)
		}
		if last != string(ReasonShieldCapReached) {
			t.Fatalf("decide.block_reason after the cap = %q, want shield_cap_reached", last)
		}
		if n := shieldWritesTo(h.sink, "A"); n != 3 {
			t.Fatalf("grant_shield writes to A = %d, want 3", n)
		}
	})
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_CooldownBoundary(t *testing.T) {
	// The default 5 s shield written at t0 ends at t0 + 5 s + 2 s margin; with a 15 s
	// cooldown the next grant is refused until t0 + 22 s.
	for _, tc := range []struct {
		name   string
		offset time.Duration
		want   string
	}{
		{"UC-040/A33 refused just before the end of the cooldown", 22*time.Second - time.Millisecond, "grant_shield/WEAK:shield_cooldown"},
		{"UC-040/A33 admitted at the end of the cooldown", 22 * time.Second, "grant_shield/WEAK:dispatched"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := limitLoop(t, 100, 0, 15, budgetShield("WEAK"))
			start := *clock
			_ = runCycle(l, budgetCtx())
			*clock = start.Add(tc.offset)
			if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{tc.want}) {
				t.Fatalf("outcomes = %v, want %s", got, tc.want)
			}
		})
	}
	t.Run("UC-040/A33 the end uses the granted seconds", func(t *testing.T) {
		long := budgetShield("WEAK")
		long.Value = "10"
		l, _, clock, _ := limitLoop(t, 100, 0, 15, long)
		start := *clock
		_ = runCycle(l, budgetCtx())
		*clock = start.Add(27*time.Second - time.Millisecond)
		if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:shield_cooldown"}) {
			t.Fatalf("outcomes = %v, want shield_cooldown until 10 s + 2 s + 15 s", got)
		}
	})
	t.Run("UC-040/A33 another player is not paced", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 100, 0, 15, budgetShield("WEAK"))
		_ = runCycle(l, budgetCtx())
		l.Rules = fakeRules{out: []Decision{budgetShield("S")}}
		*clock = clock.Add(time.Second)
		if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/S:dispatched"}) {
			t.Fatalf("outcomes = %v, want S dispatched", got)
		}
	})
}

// UC: UC-040 A33 A11 A27 BR-022
func TestShieldLimits_ReleasedWhenNotWritten(t *testing.T) {
	for _, tc := range []struct {
		name string
		sink ActionSink
	}{
		{"UC-040/A33 UC-040/A11 a gated-off write does not count", &gatedSink{gateOn: false}},
		{"UC-040/A33 UC-040/A27 a failed write does not count", errSink{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, clock, _ := limitLoop(t, 100, 1, 15, budgetShield("WEAK"))
			l.Actions = tc.sink
			_ = runCycle(l, budgetCtx())
			*clock = clock.Add(time.Second)
			l.Actions = &capturingSink{}
			if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("next grant outcomes = %v, want dispatched (nothing was written: no count, no cooldown)", got)
			}
		})
	}
	t.Run("UC-040/A33 a rate-limit block after the reservation does not count", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 2, 1, 15, budgetShield("WEAK"))
		l.limiter.allow(clock.Add(-rateWindow+500*time.Millisecond), 2, 2) // full now, frees in 0.5 s
		if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:rate_limit"}) {
			t.Fatalf("first grant outcomes = %v, want rate_limit", got)
		}
		*clock = clock.Add(time.Second)
		if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("next grant outcomes = %v, want dispatched (the rate limit released the reservation)", got)
		}
	})
}

// UC: UC-040 A31 A33 BR-020 BR-022
func TestShieldLimits_RefusalLeaksNoCharge(t *testing.T) {
	t.Run("UC-040/A33 a cap refusal in trigger mode leaves ChargedSerials empty", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 100, 1, 0, budgetShield("WEAK"))
		l.SetShieldChargeMode(triggerMode(20*time.Second, nil))
		start := *clock
		_ = runCycle(l, ffaRealCtx())
		*clock = start.Add(22 * time.Second) // the charge window is over
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:shield_cap_reached"}) {
			t.Fatalf("second grant outcomes = %v, want shield_cap_reached", got)
		}
		if charged := l.ChargedSerials(); len(charged) != 0 {
			t.Fatalf("ChargedSerials = %v after a cap refusal, want empty (no charge reserved)", charged)
		}
		st := l.buildDecideRequest(ffaRealCtx(), allInterventions(), false).State
		if chargedIn(t, st, "WEAK") {
			t.Fatal("the decide state must not mark WEAK shield_charged after a cap refusal")
		}
	})
	t.Run("UC-040/A31 a rate-limit block in trigger mode leaves ChargedSerials empty", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 2, 3, 15, budgetShield("WEAK"))
		l.SetShieldChargeMode(triggerMode(20*time.Second, nil))
		l.limiter.allow(clock.Add(-rateWindow+30*time.Second), 2, 2)
		if got := outcomeSummary(runCycle(l, ffaRealCtx())); !equalStrings(got, []string{"grant_shield/WEAK:rate_limit"}) {
			t.Fatalf("outcomes = %v, want rate_limit", got)
		}
		if charged := l.ChargedSerials(); len(charged) != 0 {
			t.Fatalf("ChargedSerials = %v after a rate-limit block, want empty", charged)
		}
	})
}

// blockingSink holds the FIRST Apply until release is closed, so that write stays in
// flight; later Applies go through at once, so a grant the ledger wrongly admits
// fails the assertion instead of hanging the test.
type blockingSink struct {
	capturingSink
	entered chan struct{}
	release chan struct{}
	once    sync.Once
}

func (s *blockingSink) Apply(ctx context.Context, d Decision) error {
	first := false
	s.once.Do(func() { first = true; close(s.entered) })
	if first {
		<-s.release
	}
	return s.capturingSink.Apply(ctx, d)
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_PendingBlocksCooldown(t *testing.T) {
	t.Run("UC-040/A33 a grant still being written refuses a second grant shield_cooldown", func(t *testing.T) {
		l, _, clock, _ := limitLoop(t, 100, 3, 15)
		sink := &blockingSink{entered: make(chan struct{}), release: make(chan struct{})}
		l.Actions = sink
		snap := l.Flags.Evaluate(context.Background())
		c := budgetCtx()
		done := make(chan struct{})
		go func() {
			defer close(done)
			var st LayerState
			l.runDecision(context.Background(), snap, c, budgetShield("WEAK"), *clock, &st)
		}()
		<-sink.entered
		var st LayerState
		l.runDecision(context.Background(), snap, c, budgetShield("WEAK"), *clock, &st)
		close(sink.release)
		<-done
		if got := outcomeSummary(st); !equalStrings(got, []string{"grant_shield/WEAK:shield_cooldown"}) {
			t.Fatalf("concurrent grant outcomes = %v, want shield_cooldown", got)
		}
		if n := shieldWritesTo(&sink.capturingSink, "WEAK"); n != 1 {
			t.Fatalf("writes = %d, want 1", n)
		}
	})
	t.Run("UC-040/A33 with the cooldown off a pending grant still counts against the cap", func(t *testing.T) {
		var cl chargeLedger
		now := time.Unix(1_000, 0)
		req := shieldRequest{limits: shieldLimits{maxShields: 1}, seconds: 5 * time.Second}
		if res, _, _ := cl.reserve("WEAK", now, req); res == nil {
			t.Fatal("first reserve refused")
		}
		if res, reason, _ := cl.reserve("WEAK", now, req); res != nil || reason != ReasonShieldCapReached {
			t.Fatalf("second reserve = %v / %q, want shield_cap_reached", res, reason)
		}
	})
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_LastEndKeepsLater(t *testing.T) {
	t.Run("UC-040/A33 a shorter grant committed later does not move the cooldown start back", func(t *testing.T) {
		var cl chargeLedger
		t0 := time.Unix(1_000, 0)
		off := shieldLimits{}
		long := shieldRequest{limits: off, seconds: 30 * time.Second}
		short := shieldRequest{limits: off, seconds: 2 * time.Second}
		resLong, _, _ := cl.reserve("WEAK", t0, long)
		resShort, _, _ := cl.reserve("WEAK", t0, short)
		if resLong == nil || resShort == nil {
			t.Fatal("precondition: with the cooldown off both grants must be in flight")
		}
		cl.commit("WEAK", resLong.gen, t0, long)
		cl.commit("WEAK", resShort.gen, t0.Add(time.Second), short)
		// long ends at t0+32 s; short would end at t0+5 s.
		on := shieldRequest{limits: shieldLimits{cooldown: 10 * time.Second}, seconds: 2 * time.Second}
		if res, reason, _ := cl.reserve("WEAK", t0.Add(40*time.Second), on); res != nil || reason != ReasonShieldCooldown {
			t.Fatalf("reserve at t0+40s = %v / %q, want shield_cooldown (runs to t0+42s)", res, reason)
		}
		if res, _, _ := cl.reserve("WEAK", t0.Add(42*time.Second), on); res == nil {
			t.Fatal("reserve at t0+42s refused, want the cooldown over")
		}
	})
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_Concurrent(t *testing.T) {
	t.Run("UC-040/A33 concurrent grants to one player never exceed the cap", func(t *testing.T) {
		l, sink, clock, _ := limitLoop(t, 1000, 3, 0)
		snap := l.Flags.Evaluate(context.Background())
		c := budgetCtx()
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := 0; i < 32; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				var state LayerState
				<-start
				l.runDecision(context.Background(), snap, c, budgetShield("WEAK"), *clock, &state)
			}()
		}
		close(start)
		wg.Wait()
		if n := shieldWritesTo(sink, "WEAK"); n != 3 {
			t.Fatalf("writes = %d, want exactly 3", n)
		}
	})
}

// UC: UC-040 A33 A21 BR-022
func TestShieldLimits_RepeatRefusalQuiet(t *testing.T) {
	t.Run("UC-040/A33 only the first cap refusal is emitted; repeats only count", func(t *testing.T) {
		l, _, clock, decisionSpans := limitLoop(t, 100, 1, 0, budgetShield("WEAK"))
		reader := metric.NewManualReader()
		l.decisions = newDecisionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		logs := &syncBuffer{}
		l.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

		_ = runCycle(l, budgetCtx())
		const refusals = 3
		for i := 0; i < refusals; i++ {
			*clock = clock.Add(2 * time.Second)
			if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:shield_cap_reached"}) {
				t.Fatalf("refusal %d outcomes = %v, want shield_cap_reached", i, got)
			}
		}
		if n := decisionSpans(); n != 2 {
			t.Fatalf("agent.decision spans = %d, want 2 (the grant and the first refusal)", n)
		}
		if n := logs.count("agent.decision_blocked"); n != 1 {
			t.Errorf("agent.decision_blocked warnings = %d, want 1", n)
		}
		if n := logs.count("level=DEBUG msg=agent.decision_shield_cap_reached_repeat"); n != refusals-1 {
			t.Errorf("repeat debug lines = %d, want %d", n, refusals-1)
		}
		if got := decisionsByLabels(t, reader)["grant_shield/true/shield_cap_reached"]; got != refusals {
			t.Errorf("agent_decisions_total{grant_shield,true,shield_cap_reached} = %d, want %d", got, refusals)
		}
	})
	t.Run("UC-040/A33 each new cooldown reports its first refusal", func(t *testing.T) {
		l, _, clock, decisionSpans := limitLoop(t, 100, 0, 15, budgetShield("WEAK"))
		for grant := 0; grant < 2; grant++ {
			_ = runCycle(l, budgetCtx())
			for i := 0; i < 3; i++ {
				*clock = clock.Add(time.Second)
				_ = runCycle(l, budgetCtx())
			}
			*clock = clock.Add(30 * time.Second)
		}
		if n := decisionSpans(); n != 4 {
			t.Fatalf("agent.decision spans = %d, want 4 (two grants, one first refusal each)", n)
		}
	})
}

// UC: UC-040 A33 BR-022
func TestShieldLimits_NewGameAndInert(t *testing.T) {
	t.Run("UC-040/A33 BR-022 a new game starts with no shields counted", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Policy.MaxShieldsPerPlayerPerGame = 1
		sinks := map[string]*capturingSink{}
		set := NewLoopSet(func(gameID string) *Loop {
			l, _ := decideRecordingLoop(t, snap)
			l.Rules = fakeRules{out: []Decision{budgetShield("WEAK")}}
			sinks[gameID] = &capturingSink{}
			l.Actions = sinks[gameID]
			return l
		})
		g1 := set.For("g1")
		for i := 0; i < 2; i++ {
			_ = runCycle(g1, budgetCtx())
		}
		if got := outcomeSummary(runCycle(g1, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:shield_cap_reached"}) {
			t.Fatalf("game 1 outcomes = %v, want shield_cap_reached", got)
		}
		if got := outcomeSummary(runCycle(set.For("g2"), budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
			t.Fatalf("game 2 outcomes = %v, want dispatched (a new game counts from 0)", got)
		}
	})
	t.Run("UC-040/A33 0/0 refuses nothing and the views stay empty", func(t *testing.T) {
		l, sink, clock, _ := limitLoop(t, 1000, 0, 0, budgetShield("WEAK"))
		for i := 0; i < 10; i++ {
			if got := outcomeSummary(runCycle(l, budgetCtx())); !equalStrings(got, []string{"grant_shield/WEAK:dispatched"}) {
				t.Fatalf("grant %d outcomes = %v, want dispatched", i, got)
			}
			*clock = clock.Add(time.Second)
		}
		if n := shieldWritesTo(sink, "WEAK"); n != 10 {
			t.Fatalf("writes = %d, want 10", n)
		}
		if charged, noShield := l.ShieldSerials(); charged != nil || noShield != nil {
			t.Fatalf("views = %v / %v, want nil / nil", charged, noShield)
		}
	})
	t.Run("UC-040/A33 a capped player is not marked shield_charged", func(t *testing.T) {
		l, _, _, _ := limitLoop(t, 100, 1, 0, budgetShield("WEAK"))
		_ = runCycle(l, budgetCtx())
		got := l.buildDecideRequest(budgetCtx(), allInterventions(), false).State
		want := decide.BuildState(budgetCtx())
		if got.Digest != want.Digest {
			t.Fatalf("decide state digest changed for a capped player: %q, want %q", got.Digest, want.Digest)
		}
	})
	t.Run("UC-040/A33 shield seconds fall back to the default", func(t *testing.T) {
		for _, v := range []string{"", "abc", "-1", "NaN"} {
			if got := shieldSeconds(Decision{Value: v}); got != DefaultShieldSeconds*time.Second {
				t.Errorf("shieldSeconds(%q) = %v, want %v", v, got, DefaultShieldSeconds*time.Second)
			}
		}
		if got := shieldSeconds(Decision{Value: "6.5"}); got != 6500*time.Millisecond {
			t.Errorf("shieldSeconds(6.5) = %v", got)
		}
	})
}
