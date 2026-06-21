package decision

import (
	"context"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// upgrade_gate_test.go covers the #1207 rules-first in-game decision gate: by default
// mode="llm" decides IMMEDIATELY via rules, and the async LLM is fired only as an
// explicit, length-gated UPGRADE. The gate has two conditions, both required:
//
//	1. master switch llm.in_game_async_upgrade ON, and
//	2. expected_seconds (= ActivePlayerCount × seconds_per_player × style_factor) at
//	   least the existing latency budget.
//
// The first three sub-tests exercise shouldUpgradeToAsyncLLM directly (switch off →
// rules; short game → rules; long game → fires). The last drives the full decide()
// path through the async-wired loop to prove a sub-budget game NEVER fires the backend
// (rules decided synchronously) while a long one DOES.

// upgradeSnapshot is an llm-mode snapshot whose #847 call gate always admits, with the
// #1207 upgrade switch ON and an 8s latency budget (the production default). The two
// estimate parameters are caller-tunable per sub-test.
func upgradeSnapshot(secondsPerPlayer, styleFactor float64) flags.Snapshot {
	s := llmDecideSnapshot()
	s.LLMGate.LatencyBudget = 8 * time.Second
	s.LLMGate.InGameAsyncUpgrade = true
	s.LLMGate.SecondsPerPlayer = secondsPerPlayer
	s.LLMGate.StyleFactor = styleFactor
	return s
}

// ctxWithActivePlayers builds an active real-game context whose Session.ActivePlayerCount
// is set to n — the signal the length estimate reads.
func ctxWithActivePlayers(n int) gamecontext.GameContext {
	return gamecontext.GameContext{
		SessionID: "g1",
		GameKind:  "real",
		Session:   gamecontext.SessionSignals{GameActive: boolPtr(true), ActivePlayerCount: &n},
	}
}

func TestUpgradeGate_SwitchOff_RulesOnly(t *testing.T) {
	l := NewLoop(nil, nil)
	s := upgradeSnapshot(10, 1.0) // 8 players would easily clear the budget...
	s.LLMGate.InGameAsyncUpgrade = false
	// ...but the master switch is OFF, so the gate must refuse regardless of length.
	if l.shouldUpgradeToAsyncLLM(s, ctxWithActivePlayers(8)) {
		t.Fatal("upgrade gate fired with the master switch OFF — mode=llm must be rules-first")
	}
}

func TestUpgradeGate_ShortGame_RulesOnly(t *testing.T) {
	l := NewLoop(nil, nil)
	// 2 players × 3s × 1.0 = 6s expected < 8s budget → a short (shadow-sized) game.
	s := upgradeSnapshot(3, 1.0)
	if l.shouldUpgradeToAsyncLLM(s, ctxWithActivePlayers(2)) {
		t.Fatal("upgrade gate fired on a sub-budget game — short games must stay rules-only")
	}
}

func TestUpgradeGate_LongGame_Fires(t *testing.T) {
	l := NewLoop(nil, nil)
	// 8 players × 3s × 1.0 = 24s expected ≥ 8s budget → a long endurance game.
	s := upgradeSnapshot(3, 1.0)
	if !l.shouldUpgradeToAsyncLLM(s, ctxWithActivePlayers(8)) {
		t.Fatal("upgrade gate refused a game whose expected length clears the budget")
	}
}

// TestUpgradeGate_BoundaryExactlyBudgetFires pins the inclusive comparison: an estimate
// EXACTLY equal to the budget fires (expected_seconds ≥ budget).
func TestUpgradeGate_BoundaryExactlyBudgetFires(t *testing.T) {
	l := NewLoop(nil, nil)
	// 4 players × 2s × 1.0 = 8s == 8s budget.
	s := upgradeSnapshot(2, 1.0)
	if !l.shouldUpgradeToAsyncLLM(s, ctxWithActivePlayers(4)) {
		t.Fatal("upgrade gate refused an estimate EXACTLY at the budget (comparison must be ≥)")
	}
}

// TestUpgradeGate_StyleFactorShiftsCrossover shows the style knob moving the decision:
// the same 2-player game is rules-only at the balanced 1.0 factor but fires at the
// aggressive 1.5 factor (2 × 3 × 1.5 = 9 ≥ 8).
func TestUpgradeGate_StyleFactorShiftsCrossover(t *testing.T) {
	l := NewLoop(nil, nil)
	if l.shouldUpgradeToAsyncLLM(upgradeSnapshot(3, 1.0), ctxWithActivePlayers(2)) {
		t.Fatal("balanced style_factor should keep the 2-player game rules-only")
	}
	if !l.shouldUpgradeToAsyncLLM(upgradeSnapshot(3, 1.5), ctxWithActivePlayers(2)) {
		t.Fatal("aggressive style_factor should let the 2-player game upgrade to LLM")
	}
}

// TestExpectedGameSeconds_FallsBackToPlayerCount proves the nil-ActivePlayerCount
// fallback uses len(c.Players).
func TestExpectedGameSeconds_FallsBackToPlayerCount(t *testing.T) {
	c := gamecontext.GameContext{
		Players: map[string]*gamecontext.PlayerSignals{
			"A": {Serial: "A"},
			"B": {Serial: "B"},
			"C": {Serial: "C"},
		},
	}
	// ActivePlayerCount is nil → fall back to 3 known players: 3 × 3 × 1 = 9.
	if got := expectedGameSeconds(c, 3, 1.0); got != 9 {
		t.Fatalf("expectedGameSeconds with nil ActivePlayerCount = %v, want 9 (len(Players) fallback)", got)
	}
	// With ActivePlayerCount set it takes precedence over the map size: 5 × 3 × 1 = 15.
	n := 5
	c.Session.ActivePlayerCount = &n
	if got := expectedGameSeconds(c, 3, 1.0); got != 15 {
		t.Fatalf("expectedGameSeconds with ActivePlayerCount=5 = %v, want 15", got)
	}
}

// TestUpgradeGate_DecidePath_ShortGameRulesOnly drives the FULL decide() path through an
// async-wired loop: a sub-budget game must NOT fire the backend — the rules engine
// decides synchronously this very cycle (rules-first).
func TestUpgradeGate_DecidePath_ShortGameRulesOnly(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", ctxWithActivePlayers(2))
	// 2 players × 3s × 1.0 = 6s < 8s budget.
	l, _, sink := asyncLoop(t, upgradeSnapshot(3, 1.0), resolverWith(be), provider, "g1",
		[]Decision{{Intervention: "grant_shield", Reason: "from rules"}})

	l.OnEvaluate(context.Background(), ctxWithActivePlayers(2), testTrigger())
	l.AwaitInflight()

	if be.callCount() != 0 {
		t.Fatalf("backend Infer calls = %d, want 0 (short game must be rules-only, no async fire)", be.callCount())
	}
	if sink.calls.Load() != 1 {
		t.Fatalf("action sink calls = %d, want 1 (rules decided synchronously)", sink.calls.Load())
	}
}

// TestUpgradeGate_DecidePath_LongGameFires is the positive twin: a game whose estimate
// clears the budget DOES fire the async backend.
func TestUpgradeGate_DecidePath_LongGameFires(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	provider.set("g1", ctxWithActivePlayers(8))
	// 8 players × 3s × 1.0 = 24s ≥ 8s budget.
	l, _, _ := asyncLoop(t, upgradeSnapshot(3, 1.0), resolverWith(be), provider, "g1", nil)

	l.OnEvaluate(context.Background(), ctxWithActivePlayers(8), testTrigger())

	// The fire happens in a goroutine; let it start, release, and join.
	select {
	case <-be.started:
	case <-time.After(2 * time.Second):
		t.Fatal("backend Infer never started — the long game did not fire the async LLM upgrade")
	}
	close(be.release)
	l.AwaitInflight()

	if be.callCount() != 1 {
		t.Fatalf("backend Infer calls = %d, want 1 (long game fires the async LLM upgrade)", be.callCount())
	}
}

// TestUpgradeGate_LateLLMIsSeparateIntervention_RateLimited is the #1207 double-fire
// proof and the reversal of reason fork (B) in async_infer.go:24-49. On a long game the
// async LLM upgrade fires; its late result lands AFTER rules could already have acted in
// a prior cycle, and it dispatches through the SAME runDecision chain as its OWN
// intervention — INTENDED, and bounded by policy.max_interventions_per_minute. Here the
// single rate slot is pre-spent (modeling the prior rules intervention), so the late LLM
// result is rate-limited at apply time and discarded: the limiter — not a new suppression
// rule — is what bounds the second fire within one context.
func TestUpgradeGate_LateLLMIsSeparateIntervention_RateLimited(t *testing.T) {
	be := newBlockingBackend("phi4-mini", validShieldResponse)
	provider := newFakeContextProvider()
	long := ctxWithActivePlayers(8) // 24s ≥ 8s budget → fires
	long.Players = map[string]*gamecontext.PlayerSignals{"AAAA": {Serial: "AAAA", Active: boolPtr(true)}}
	provider.set("g1", long)

	snap := upgradeSnapshot(3, 1.0)
	snap.Policy.MaxInterventionsPerMinute = 1 // one weighted slot shared by rules + late LLM
	clock := time.Unix(5000, 0)
	l, sr, sink := asyncLoop(t, snap, resolverWith(be), provider, "g1", nil)
	l.now = func() time.Time { return clock }

	// Model the EARLIER rules intervention having already consumed the single slot. The
	// late LLM result is a SECOND fire in the same context; the rate limiter must bound it.
	if !l.limiter.allow(clock, interventionCost("grant_shield"), 1) {
		t.Fatal("expected the first (rules) slot to be available")
	}

	l.OnEvaluate(context.Background(), long, testTrigger())
	close(be.release)
	l.AwaitInflight()

	if sink.calls.Load() != 0 {
		t.Errorf("action sink calls = %d, want 0 (late LLM is a separate intervention, rate-bounded)", sink.calls.Load())
	}
	if be.callCount() != 1 {
		t.Errorf("backend Infer calls = %d, want 1 (the upgrade DID fire; the limiter bounds the apply)", be.callCount())
	}
	// The late result went through the runDecision chain and was bounded by the limiter,
	// not suppressed by a new in-context rule.
	assertDiscard(t, sr, DiscardRateLimited)
}
