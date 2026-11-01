package decision

import (
	"context"
	"log/slog"
	"math/rand/v2"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/sdk/metric"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// appliedledger_uc040_test.go covers the already-applied suppression (UC-040 A35,
// BR-023; decide-contract §4.5 step 3; #2046): a state or level decision that repeats
// the value the loop last wrote is blocked already_applied before the rate limit,
// with no write and no budget; grants and events are never suppressed; a failed or
// overlapping write deletes the entry; the entry expires after 30 s and is dropped
// when its player leaves; a loop without suppression is inert; the rules engine drops
// an already-applied candidate before ranking.

func sens(target, value string) Decision {
	return Decision{Intervention: InterventionAdjustPlayerSensitivity, TargetSerial: target, Value: value,
		ObjectiveServed: ObjectiveBalanced, Objectives: balancedFocused()}
}

// appliedLoop is budgetLoop with the already-applied suppression on.
func appliedLoop(t *testing.T, out ...Decision) (*Loop, *capturingSink, *time.Time, func() int) {
	t.Helper()
	l, sink, clock, spans := budgetLoop(t, 100, out...)
	l.SetSuppressReapplied(true)
	return l, sink, clock, spans
}

func writesOf(sink *capturingSink, intervention, serial string) int {
	n := 0
	for _, d := range sink.decisions() {
		if d.Intervention == intervention && d.TargetSerial == serial {
			n++
		}
	}
	return n
}

func expectOutcome(t *testing.T, st LayerState, want string) {
	t.Helper()
	if got := outcomeSummary(st); !equalStrings(got, []string{want}) {
		t.Fatalf("outcomes = %v, want %s", got, want)
	}
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_RepeatBlockedNoWriteNoBudget(t *testing.T) {
	t.Run("UC-040/A35 BR-023 a repeated 0.8 is blocked already_applied, not written, no budget", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
		before := limiterEntries(l)
		for _, v := range []string{"0.8", "0.80", " 0.8 "} {
			*clock = clock.Add(time.Second)
			l.Rules = fakeRules{out: []Decision{sens("WEAK", v)}}
			expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:already_applied")
		}
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "WEAK"); n != 1 {
			t.Fatalf("writes = %d, want 1", n)
		}
		if after := limiterEntries(l); after != before {
			t.Fatalf("limiter entries %d -> %d: already_applied must not charge the budget", before, after)
		}
	})
	t.Run("UC-040/A35 BR-014 already_applied does not end rank-order admission", func(t *testing.T) {
		// Budget 1: the repeat ranked first must not hold the cycle, so the shield
		// ranked second still fits.
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		snap := l.Flags.Evaluate(context.Background())
		snap.Policy.MaxInterventionsPerMinute = 1
		_ = runCycle(l, budgetCtx())
		l.limiter = rateLimiter{}
		l.Flags = fakeFlags{snap: snap}
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "0.8"), budgetShield("S")}}
		got := outcomeSummary(runCycle(l, budgetCtx()))
		if !equalStrings(got, []string{"adjust_player_sensitivity/WEAK:already_applied", "grant_shield/S:dispatched"}) {
			t.Fatalf("outcomes = %v, want the repeat suppressed and the shield dispatched", got)
		}
		if n := writesOf(sink, InterventionGrantShield, "S"); n != 1 {
			t.Fatalf("shield writes = %d, want 1", n)
		}
	})
}

// UC: UC-040 A35 A21 BR-023
func TestAlreadyApplied_FirstInfoThenQuiet(t *testing.T) {
	t.Run("UC-040/A35 the first suppression logs info and says already in place; repeats only count", func(t *testing.T) {
		l, _, clock, decisionSpans := appliedLoop(t, sens("WEAK", "0.8"))
		reader := metric.NewManualReader()
		l.decisions = newDecisionsCounter(metric.NewMeterProvider(metric.WithReader(reader)))
		logs := &syncBuffer{}
		l.Log = slog.New(slog.NewTextHandler(logs, &slog.HandlerOptions{Level: slog.LevelDebug}))
		var inPlace, blockedTimeline int
		l.SetAlreadyAppliedRecorder(func(intervention, serial string) { inPlace++ })
		l.SetInterventionRecorder(func(intervention, serial string, blocked bool) {
			if blocked {
				blockedTimeline++
			}
		})

		_ = runCycle(l, budgetCtx())
		const repeats = 3
		for i := 0; i < repeats; i++ {
			*clock = clock.Add(time.Second)
			_ = runCycle(l, budgetCtx())
		}
		if n := decisionSpans(); n != 2 {
			t.Fatalf("agent.decision spans = %d, want 2 (the write and the first suppression)", n)
		}
		if n := logs.count("level=INFO msg=agent.decision_already_applied "); n != 1 {
			t.Errorf("info agent.decision_already_applied lines = %d, want 1", n)
		}
		if n := logs.count("agent.decision_blocked"); n != 0 {
			t.Errorf("agent.decision_blocked warnings = %d, want 0: already_applied is not a refusal", n)
		}
		if n := logs.count("level=DEBUG msg=agent.decision_already_applied_repeat"); n != repeats-1 {
			t.Errorf("repeat debug lines = %d, want %d", n, repeats-1)
		}
		if inPlace != 1 || blockedTimeline != 0 {
			t.Errorf("timeline: already in place = %d, blocked = %d; want 1 and 0", inPlace, blockedTimeline)
		}
		if got := decisionsByLabels(t, reader)["adjust_player_sensitivity/true/already_applied"]; got != repeats {
			t.Errorf("agent_decisions_total{already_applied} = %d, want %d", got, repeats)
		}
	})
	t.Run("UC-040/A35 the timeline renders already in place", func(t *testing.T) {
		now := time.Unix(1_000, 0)
		s := gamecontext.NewStore(time.Hour, time.Hour, func() time.Time { return now })
		s.AppendInterventionInPlace(InterventionAdjustPlayerSensitivity, "WEAK")
		lines := gamecontext.RenderTimeline(s.Snapshot().Timeline, now)
		if len(lines) != 1 || !strings.Contains(lines[0], "adjust_player_sensitivity already in place -> WEAK") {
			t.Fatalf("timeline = %v, want one 'already in place' line", lines)
		}
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_NewValueWritten(t *testing.T) {
	t.Run("UC-040/A35 0.8 then 1.5 is written", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "1.5")}}
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "WEAK"); n != 2 {
			t.Fatalf("writes = %d, want 2", n)
		}
	})
	t.Run("UC-040/A35 another player's key is independent", func(t *testing.T) {
		l, _, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{sens("S", "0.8")}}
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/S:dispatched")
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_GrantsAndEventsNeverSuppressed(t *testing.T) {
	var others []string
	for i := range interventionWeights {
		if !idempotentInterventions[i] {
			others = append(others, i)
		}
	}
	sort.Strings(others)
	if len(others) != 9 {
		t.Fatalf("grants and events = %v, want the 9 of registry kinds grant and event", others)
	}
	for _, intervention := range others {
		if intervention == InterventionGrantGroupShield {
			// Needs the group gate and a set (#2467); never suppressed either:
			// group_uc040_test.go writes two group shields in one game.
			continue
		}
		t.Run("UC-040/BR-023 "+intervention+" is written every time", func(t *testing.T) {
			d := Decision{Intervention: intervention, TargetSerial: "WEAK", Value: "5", Objectives: balancedFocused()}
			l, sink, clock, _ := appliedLoop(t, d)
			for i := 0; i < 2; i++ {
				expectOutcome(t, runCycle(l, budgetCtx()), intervention+"/WEAK:dispatched")
				*clock = clock.Add(time.Second)
			}
			if n := writesOf(sink, intervention, "WEAK"); n != 2 {
				t.Fatalf("writes = %d, want 2", n)
			}
		})
	}
	t.Run("UC-040/BR-023 every state and level intervention is suppressed", func(t *testing.T) {
		for _, intervention := range IdempotentInterventions() {
			d := Decision{Intervention: intervention, TargetSerial: "WEAK", Value: "1.1", Objectives: balancedFocused()}
			l, _, clock, _ := appliedLoop(t, d)
			_ = runCycle(l, budgetCtx())
			*clock = clock.Add(time.Second)
			expectOutcome(t, runCycle(l, budgetCtx()), intervention+"/WEAK:already_applied")
		}
	})
}

// UC: UC-040 A35 A27 BR-023
func TestAlreadyApplied_FailedWriteDeletesEntry(t *testing.T) {
	t.Run("UC-040/A35 UC-040/A27 a failed write deletes the entry, so the old value is written again", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		// 1.5 fails: the flag service may still have applied it, so 0.8 is unknown now.
		l.Actions = errSink{}
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "1.5")}}
		*clock = clock.Add(time.Second)
		_ = runCycle(l, budgetCtx())
		l.Actions = sink
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "0.8")}}
		*clock = clock.Add(time.Second)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
	})
	t.Run("UC-040/A35 UC-040/A27 a failed write records nothing, so its value is written again", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		l.Actions = errSink{}
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "1.5")}}
		*clock = clock.Add(time.Second)
		_ = runCycle(l, budgetCtx())
		l.Actions = sink
		*clock = clock.Add(time.Second)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
	})
	t.Run("UC-040/A35 UC-040/A11 a gated write leaves the entry as it was", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		l.Actions = &gatedSink{gateOn: false}
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "1.5")}}
		*clock = clock.Add(time.Second)
		_ = runCycle(l, budgetCtx())
		l.Actions = sink
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "0.8")}}
		*clock = clock.Add(time.Second)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:already_applied")
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_OverlapDeletesEntry(t *testing.T) {
	t.Run("UC-040/A35 two writes to one key in flight at once leave no entry", func(t *testing.T) {
		l, _, clock, _ := appliedLoop(t)
		sink := &blockingSink{entered: make(chan struct{}), release: make(chan struct{})}
		l.Actions = sink
		snap := l.Flags.Evaluate(context.Background())
		c := budgetCtx()
		done := make(chan struct{})
		go func() {
			defer close(done)
			var st LayerState
			l.runDecision(context.Background(), snap, c, sens("WEAK", "0.8"), *clock, &st)
		}()
		<-sink.entered
		var st LayerState
		l.runDecision(context.Background(), snap, c, sens("WEAK", "1.5"), *clock, &st)
		close(sink.release)
		<-done
		// 0.8 finished last, but the order on the flag service is unknown: no entry.
		*clock = clock.Add(time.Second)
		var next LayerState
		l.runDecision(context.Background(), snap, c, sens("WEAK", "0.8"), *clock, &next)
		expectOutcome(t, next, "adjust_player_sensitivity/WEAK:dispatched")
	})
	t.Run("UC-040/A35 a recorded value is written again while a different value is in flight", func(t *testing.T) {
		l, _, clock, _ := appliedLoop(t)
		snap := l.Flags.Evaluate(context.Background())
		c := budgetCtx()
		var rec LayerState
		l.runDecision(context.Background(), snap, c, sens("WEAK", "0.8"), *clock, &rec)
		expectOutcome(t, rec, "adjust_player_sensitivity/WEAK:dispatched")
		sink := &blockingSink{entered: make(chan struct{}), release: make(chan struct{})}
		l.Actions = sink
		done := make(chan struct{})
		go func() {
			defer close(done)
			var st LayerState
			l.runDecision(context.Background(), snap, c, sens("WEAK", "1.5"), *clock, &st)
		}()
		<-sink.entered
		if _, ok := l.AppliedValues()[appliedKey(sens("WEAK", "0.8"))]; ok {
			t.Errorf("AppliedValues lists WEAK while 1.5 is in flight; the rules would drop 0.8")
		}
		// 1.5 may land after this decision: the value on the flag service is unknown.
		var next LayerState
		l.runDecision(context.Background(), snap, c, sens("WEAK", "0.8"), *clock, &next)
		close(sink.release)
		<-done
		expectOutcome(t, next, "adjust_player_sensitivity/WEAK:dispatched")
	})
	t.Run("UC-040/A35 concurrent identical decisions are all safe under -race", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t)
		snap := l.Flags.Evaluate(context.Background())
		c := budgetCtx()
		var wg sync.WaitGroup
		for i := 0; i < 16; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				var st LayerState
				l.runDecision(context.Background(), snap, c, sens("WEAK", "0.8"), *clock, &st)
				_ = l.AppliedValues()
			}()
		}
		wg.Wait()
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "WEAK"); n < 1 {
			t.Fatalf("writes = %d, want at least 1", n)
		}
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_TTLReasserts(t *testing.T) {
	t.Run("UC-040/A35 BR-023 the same value is written again 30 s after the write", func(t *testing.T) {
		l, sink, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		start := *clock
		_ = runCycle(l, budgetCtx())
		*clock = start.Add(appliedLedgerTTL - time.Millisecond)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:already_applied")
		*clock = start.Add(appliedLedgerTTL)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "WEAK"); n != 2 {
			t.Fatalf("writes = %d, want 2", n)
		}
		if appliedLedgerTTL != 30*time.Second {
			t.Fatalf("TTL = %v, want 30 s (BR-023)", appliedLedgerTTL)
		}
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_AbsentSerialDropped(t *testing.T) {
	without := func() gamecontext.GameContext {
		return gameCtx(40, nil, map[string]testPlayer{"S": {active: true}})
	}
	t.Run("UC-040/A35 a decision for a player no longer in the game is written", func(t *testing.T) {
		l, _, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		*clock = clock.Add(time.Second)
		expectOutcome(t, runCycle(l, without()), "adjust_player_sensitivity/WEAK:dispatched")
	})
	t.Run("UC-040/A35 an entry is dropped once its player leaves, so a rejoin is written", func(t *testing.T) {
		l, _, clock, _ := appliedLoop(t, sens("WEAK", "0.8"))
		_ = runCycle(l, budgetCtx())
		*clock = clock.Add(time.Second)
		l.Rules = fakeRules{out: []Decision{sens("S", "1.5")}}
		_ = runCycle(l, without())
		l.Rules = fakeRules{out: []Decision{sens("WEAK", "0.8")}}
		*clock = clock.Add(time.Second)
		expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_DisabledInertAndNewGame(t *testing.T) {
	t.Run("UC-040/BR-023 a loop without suppression writes every decision", func(t *testing.T) {
		l, sink, clock, _ := budgetLoop(t, 100, sens("WEAK", "0.8"))
		for i := 0; i < 3; i++ {
			expectOutcome(t, runCycle(l, budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
			*clock = clock.Add(time.Second)
		}
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "WEAK"); n != 3 {
			t.Fatalf("writes = %d, want 3", n)
		}
		if v := l.AppliedValues(); v != nil {
			t.Fatalf("AppliedValues = %v, want nil while off", v)
		}
	})
	t.Run("UC-040/A35 a new game starts with an empty record", func(t *testing.T) {
		set := NewLoopSet(func(gameID string) *Loop {
			l, _ := decideRecordingLoop(t, rulesSnapshot())
			l.Rules = fakeRules{out: []Decision{sens("WEAK", "0.8")}}
			l.Actions = &capturingSink{}
			l.SetSuppressReapplied(true)
			return l
		})
		g1 := set.For("g1")
		_ = runCycle(g1, budgetCtx())
		expectOutcome(t, runCycle(g1, budgetCtx()), "adjust_player_sensitivity/WEAK:already_applied")
		expectOutcome(t, runCycle(set.For("g2"), budgetCtx()), "adjust_player_sensitivity/WEAK:dispatched")
	})
}

// UC: UC-040 A35 BR-023
func TestAlreadyApplied_RulesPreFilter(t *testing.T) {
	t.Run("UC-040/A35 applyAppliedPolicy drops only an already-applied state or level candidate", func(t *testing.T) {
		c := budgetCtx()
		cand := func(d Decision) scoredCandidate { return scoredCandidate{candidate: candidate{decision: d}} }
		cands := []scoredCandidate{
			cand(sens("WEAK", "")),
			cand(sens("S", "")),
			cand(budgetShield("WEAK")),
			cand(sens("GONE", "")),
		}
		applied := map[string]string{
			appliedKey(sens("WEAK", "")):     "",
			appliedKey(budgetShield("WEAK")): "",
			appliedKey(sens("GONE", "")):     "",
		}
		var got []string
		for _, cd := range applyAppliedPolicy(cands, applied, c) {
			got = append(got, cd.decision.Intervention+"/"+cd.decision.TargetSerial)
		}
		want := []string{"adjust_player_sensitivity/S", "grant_shield/WEAK", "adjust_player_sensitivity/GONE"}
		if !equalStrings(got, want) {
			t.Fatalf("kept = %v, want %v", got, want)
		}
	})
	t.Run("UC-040/A35 the rules do not re-propose an applied level every second", func(t *testing.T) {
		snap := rulesSnapshot()
		snap.Objectives = balancedFocused()
		snap.Policy = flags.Policy{MaxInterventionsPerMinute: 100}
		l, _ := decideRecordingLoop(t, snap)
		clock := time.Unix(50_000, 0)
		l.now = func() time.Time { return clock }
		sink := &capturingSink{}
		l.Actions = sink
		l.SetSuppressReapplied(true)
		rules := newObjectiveRules(balancedRulesConfig(), balancedRulesConfig(), balancedRulesConfig(),
			func() time.Time { return clock }, rand.New(rand.NewPCG(1, 2)))
		rules.SetAppliedSource(l.AppliedValues)
		l.Rules = rules

		for elapsed := time.Duration(0); elapsed < appliedLedgerTTL; elapsed += 2 * time.Second {
			for _, o := range runCycle(l, chargeRulesCtx()).Outcomes {
				if o.BlockReason == ReasonAlreadyApplied {
					t.Fatalf("at %v the rules proposed an already-applied %s/%s: the pre-filter must drop it", elapsed, o.Intervention, o.TargetSerial)
				}
			}
			clock = clock.Add(2 * time.Second)
		}
		if n := writesOf(sink, InterventionAdjustPlayerSensitivity, "STRONG"); n != 1 {
			t.Fatalf("sensitivity writes to STRONG = %d, want exactly 1 within the TTL", n)
		}
	})
}
