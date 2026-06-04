package decision

import (
	"context"
	"reflect"
	"testing"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// fixedRules returns a fixed decision set.
type fixedRules struct {
	out []Decision
}

func (r *fixedRules) Evaluate(context.Context, gamecontext.GameContext) []Decision {
	return r.out
}

// ptrF is a float64 pointer helper for building GameContext players.
func ptrF(v float64) *float64 { return &v }

// loopWithClock builds a test loop with a fixed snapshot, fixed rules, recording
// actions, and an injectable clock seeded to the given time.
func loopWithClock(snap flags.Snapshot, out []Decision, clock *time.Time) (*Loop, *recordingActions) {
	actions := &recordingActions{}
	l := NewLoop(fakeFlags{snap: snap}, nil)
	l.Rules = &fixedRules{out: out}
	l.Actions = actions
	l.now = func() time.Time { return *clock }
	return l, actions
}

// --- Capability layer ---

func TestOnEvaluate_CapabilityRecordedAndPassed(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:    true,
		Mode:       "llm",
		Capability: flags.Capability{Model: "claude", PromptVariant: "aggressive"},
		Policy:     flags.Policy{MaxInterventionsPerMinute: 2, MovementVarianceWindow: 15},
	}
	loop := NewLoop(fakeFlags{snap: snap}, nil)
	loop.Rules = &fixedRules{}
	loop.Actions = &recordingActions{}

	state := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if state.Model != "claude" || state.PromptVariant != "aggressive" {
		t.Errorf("capability not recorded: model=%q prompt=%q", state.Model, state.PromptVariant)
	}
	// The variance window is recorded in the LayerState (it reaches the #726
	// engine through its PolicySource, and #731 reads it from the snapshot).
	if state.PolicyMovementVarianceWin != 15 {
		t.Errorf("LayerState.PolicyMovementVarianceWin = %d, want 15", state.PolicyMovementVarianceWin)
	}
}

// --- Battery threshold ---

func TestOnEvaluate_BatteryThreshold(t *testing.T) {
	const serial = "AA:BB"
	tests := []struct {
		name         string
		battery      *float64 // nil = unknown
		threshold    int
		wantDispatch bool
		wantReason   BlockReason
	}{
		{name: "below threshold blocks", battery: ptrF(10), threshold: 20, wantDispatch: false, wantReason: ReasonBatteryThreshold},
		{name: "at threshold dispatches", battery: ptrF(20), threshold: 20, wantDispatch: true},
		{name: "above threshold dispatches", battery: ptrF(80), threshold: 20, wantDispatch: true},
		{name: "missing battery dispatches", battery: nil, threshold: 20, wantDispatch: true},
		{name: "threshold off ignores low battery", battery: ptrF(1), threshold: 0, wantDispatch: true},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clock := time.Now()
			snap := flags.Snapshot{
				Enabled:              true,
				Mode:                 "rules",
				InterventionsAllowed: []string{"send_controller_effect"},
				Policy: flags.Policy{
					BatteryThreshold:          tc.threshold,
					MaxInterventionsPerMinute: 10,
				},
			}
			decision := Decision{Intervention: "send_controller_effect", TargetSerial: serial}
			loop, actions := loopWithClock(snap, []Decision{decision}, &clock)

			c := gamecontext.GameContext{Players: map[string]*gamecontext.PlayerSignals{
				serial: {Serial: serial, BatteryPct: tc.battery},
			}}
			state := loop.OnEvaluate(context.Background(), c, testTrigger())

			if tc.wantDispatch {
				if !actions.called {
					t.Fatalf("expected dispatch, got none")
				}
				if state.Dispatched != 1 || state.Blocked != 0 {
					t.Errorf("state dispatched=%d blocked=%d, want 1/0", state.Dispatched, state.Blocked)
				}
				return
			}
			if actions.called {
				t.Fatalf("expected block, but actions were applied")
			}
			if state.Blocked != 1 || len(state.Outcomes) != 1 {
				t.Fatalf("state blocked=%d outcomes=%d, want 1/1", state.Blocked, len(state.Outcomes))
			}
			if got := state.Outcomes[0].BlockReason; got != tc.wantReason {
				t.Errorf("block reason = %q, want %q", got, tc.wantReason)
			}
		})
	}
}

func TestOnEvaluate_BatteryThresholdSessionScopedUnaffected(t *testing.T) {
	clock := time.Now()
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"end_game"},
		Policy:               flags.Policy{BatteryThreshold: 50, MaxInterventionsPerMinute: 10},
	}
	// Session-scoped (empty TargetSerial): battery threshold must not apply.
	loop, actions := loopWithClock(snap, []Decision{{Intervention: "end_game"}}, &clock)

	state := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !actions.called || state.Dispatched != 1 {
		t.Errorf("session-scoped decision blocked by battery threshold: dispatched=%d", state.Dispatched)
	}
}

// --- Weighted rate limiter ---

func TestOnEvaluate_RateLimitWeightedBudgetExhaustion(t *testing.T) {
	clock := time.Now()
	// Budget 2/min. soft=0.5 (4 fit), then a medium=1 must still fit (total 2.0),
	// a further soft must block (2.5 > 2).
	snap := flags.Snapshot{
		Enabled: true,
		Mode:    "rules",
		InterventionsAllowed: []string{
			"play_audio_cue", "grant_shield", "eliminate_player",
		},
		Policy: flags.Policy{MaxInterventionsPerMinute: 2},
	}
	loop := NewLoop(fakeFlags{snap: snap}, nil)
	loop.now = func() time.Time { return clock }
	actions := &recordingActions{}
	loop.Actions = actions

	dispatch := func(intervention string) bool {
		actions.called = false
		loop.Rules = &fixedRules{out: []Decision{{Intervention: intervention}}}
		state := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())
		return state.Dispatched == 1
	}

	// 0.5 + 0.5 + 0.5 = 1.5 admitted.
	for i := 0; i < 3; i++ {
		if !dispatch("play_audio_cue") {
			t.Fatalf("soft #%d should fit budget", i)
		}
	}
	// medium=1 → 1.5 + 1 = 2.5 > 2: blocked.
	if dispatch("grant_shield") {
		t.Errorf("medium should be blocked (1.5 used + 1 > 2)")
	}
	// another soft → 1.5 + 0.5 = 2.0 == 2: still admitted.
	if !dispatch("play_audio_cue") {
		t.Errorf("soft should fit exactly at budget (1.5 + 0.5 = 2)")
	}
	// budget now full; any further blocks.
	if dispatch("play_audio_cue") {
		t.Errorf("budget exhausted, further soft must block")
	}
}

func TestOnEvaluate_RateLimitWindowSlides(t *testing.T) {
	clock := time.Now()
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"eliminate_player"},
		Policy:               flags.Policy{MaxInterventionsPerMinute: 2},
	}
	loop := NewLoop(fakeFlags{snap: snap}, nil)
	loop.now = func() time.Time { return clock }
	loop.Rules = &fixedRules{out: []Decision{{Intervention: "eliminate_player"}}}
	loop.Actions = &recordingActions{}

	// hard=2 fills the whole budget.
	if s := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Dispatched != 1 {
		t.Fatalf("first hard intervention should dispatch")
	}
	// Immediately after: blocked.
	if s := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Blocked != 1 {
		t.Fatalf("second hard intervention should be rate-limited")
	}
	// Advance just past the window: the first entry ages out, budget frees up.
	clock = clock.Add(rateWindow + time.Second)
	if s := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger()); s.Dispatched != 1 {
		t.Errorf("after window slide the budget should free up")
	}
}

func TestInterventionCostWeightTable(t *testing.T) {
	cases := map[string]float64{
		"play_audio_cue":            0.5,
		"send_controller_effect":    0.5,
		"adjust_volume":             0.5,
		"adjust_music_tempo":        1,
		"adjust_player_sensitivity": 1,
		"grant_shield":              1,
		"adjust_global_sensitivity": 2,
		"eliminate_player":          2,
		"revive_player":             2,
		"end_game":                  2,
		"unknown_intervention":      defaultInterventionWeight,
	}
	for intervention, want := range cases {
		if got := interventionCost(intervention); got != want {
			t.Errorf("interventionCost(%q) = %v, want %v", intervention, got, want)
		}
	}
}

// --- Blocked-reason attribution (ordering of gates) ---

func TestOnEvaluate_BlockedReasonAttribution(t *testing.T) {
	const lowSerial = "LOW"
	clock := time.Now()
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"send_controller_effect", "grant_shield"},
		Policy:               flags.Policy{BatteryThreshold: 20, MaxInterventionsPerMinute: 1},
	}
	out := []Decision{
		{Intervention: "end_game"},                                        // not on allow-list
		{Intervention: "send_controller_effect", TargetSerial: lowSerial}, // battery block
		{Intervention: "grant_shield"},                                    // dispatches (cost 1 == budget)
		{Intervention: "grant_shield"},                                    // rate-limited (budget gone)
	}
	loop, _ := loopWithClock(snap, out, &clock)
	c := gamecontext.GameContext{Players: map[string]*gamecontext.PlayerSignals{
		lowSerial: {Serial: lowSerial, BatteryPct: ptrF(5)},
	}}

	state := loop.OnEvaluate(context.Background(), c, testTrigger())

	wantReasons := []BlockReason{ReasonNotAllowed, ReasonBatteryThreshold, "", ReasonRateLimit}
	wantDispatched := []bool{false, false, true, false}
	if len(state.Outcomes) != 4 {
		t.Fatalf("got %d outcomes, want 4", len(state.Outcomes))
	}
	for i, o := range state.Outcomes {
		if o.Dispatched != wantDispatched[i] {
			t.Errorf("outcome %d dispatched=%v, want %v", i, o.Dispatched, wantDispatched[i])
		}
		if o.BlockReason != wantReasons[i] {
			t.Errorf("outcome %d reason=%q, want %q", i, o.BlockReason, wantReasons[i])
		}
	}
	if state.Dispatched != 1 || state.Blocked != 3 || state.Candidates != 4 {
		t.Errorf("counts dispatched=%d blocked=%d candidates=%d, want 1/3/4",
			state.Dispatched, state.Blocked, state.Candidates)
	}
}

// --- Full LayerState snapshot correctness ---

func TestOnEvaluate_FullLayerStateSnapshot(t *testing.T) {
	clock := time.Now()
	objectives := map[string]float64{"endurance": 0.7, "chaos": 0.3}
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		Objectives:           objectives,
		Capability:           flags.Capability{Model: "gemma3:4b", PromptVariant: "balanced"},
		InterventionsAllowed: []string{"play_audio_cue"},
		Policy: flags.Policy{
			BatteryThreshold:          25,
			MovementVarianceWindow:    12,
			MaxInterventionsPerMinute: 3,
		},
	}
	loop, _ := loopWithClock(snap, []Decision{{Intervention: "play_audio_cue"}}, &clock)

	state := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !state.Enabled || state.Mode != "rules" {
		t.Errorf("existence layer: enabled=%v mode=%q", state.Enabled, state.Mode)
	}
	if !reflect.DeepEqual(state.Objectives, objectives) {
		t.Errorf("objective layer: %v", state.Objectives)
	}
	if state.Model != "gemma3:4b" || state.PromptVariant != "balanced" {
		t.Errorf("capability layer: model=%q prompt=%q", state.Model, state.PromptVariant)
	}
	if !reflect.DeepEqual(state.InterventionsAllowed, []string{"play_audio_cue"}) {
		t.Errorf("permission layer allow-list: %v", state.InterventionsAllowed)
	}
	if state.PolicyBatteryThreshold != 25 || state.PolicyMovementVarianceWin != 12 || state.PolicyMaxPerMinute != 3 {
		t.Errorf("permission policy: battery=%d window=%d max=%d",
			state.PolicyBatteryThreshold, state.PolicyMovementVarianceWin, state.PolicyMaxPerMinute)
	}
	if state.Dispatched != 1 || len(state.Outcomes) != 1 || state.Outcomes[0].Weight != 0.5 {
		t.Errorf("outcomes: dispatched=%d outcomes=%+v", state.Dispatched, state.Outcomes)
	}

	// LastLayerState must return the same snapshot for #729.
	if !reflect.DeepEqual(loop.LastLayerState(), state) {
		t.Errorf("LastLayerState mismatch")
	}
}

func TestOnEvaluate_DisabledRecordsLayerState(t *testing.T) {
	snap := flags.Snapshot{
		Enabled:    false,
		Mode:       "rules",
		Capability: flags.Capability{Model: "phi4-mini", PromptVariant: "conservative"},
	}
	loop := NewLoop(fakeFlags{snap: snap}, nil)
	loop.Rules = &fixedRules{out: []Decision{{Intervention: "play_audio_cue"}}}
	actions := &recordingActions{}
	loop.Actions = actions

	state := loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if state.Enabled {
		t.Errorf("disabled state should record Enabled=false")
	}
	if state.Model != "phi4-mini" {
		t.Errorf("disabled state should still record capability, got %q", state.Model)
	}
	if len(state.Outcomes) != 0 || actions.called {
		t.Errorf("disabled loop must not produce outcomes or dispatch")
	}
}
