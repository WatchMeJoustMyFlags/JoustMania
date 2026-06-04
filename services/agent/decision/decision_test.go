package decision

import (
	"context"
	"reflect"
	"testing"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// fakeFlags returns a fixed snapshot for every Evaluate call.
type fakeFlags struct{ snap flags.Snapshot }

func (f fakeFlags) Evaluate(context.Context) flags.Snapshot { return f.snap }

// recordingRules records whether Evaluate was called and what objectives the
// loop published into it (via the objectivePublisher seam), and returns a
// canned decision set. It satisfies both RulesEngine and objectivePublisher.
type recordingRules struct {
	called        bool
	gotObjectives map[string]float64
	out           []Decision
}

func (r *recordingRules) Evaluate(context.Context, gamecontext.GameContext) []Decision {
	r.called = true
	return r.out
}

func (r *recordingRules) SetObjectives(weights map[string]float64) { r.gotObjectives = weights }

// recordingActions records each decision passed to Apply (now per-decision).
type recordingActions struct {
	called bool
	got    []Decision
}

func (a *recordingActions) Apply(_ context.Context, d Decision) error {
	a.called = true
	a.got = append(a.got, d)
	return nil
}

// newFlagLoop builds a Loop driven by a fixed flag snapshot, with the given
// rules and actions and a real (global no-op) tracer.
func newFlagLoop(snap flags.Snapshot, rules RulesEngine, actions ActionSink) *Loop {
	l := NewLoop(fakeFlags{snap: snap}, nil)
	l.Rules = rules
	l.Actions = actions
	return l
}

func TestOnEvaluate_KillSwitchShortCircuits(t *testing.T) {
	rules := &recordingRules{out: []Decision{{Intervention: "grant_shield"}}}
	actions := &recordingActions{}
	loop := newFlagLoop(flags.Snapshot{Enabled: false}, rules, actions)

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if rules.called {
		t.Errorf("rules evaluated despite kill switch off")
	}
	if actions.called {
		t.Errorf("actions applied despite kill switch off")
	}
}

func TestOnEvaluate_PermissionGateFiltersBlocked(t *testing.T) {
	rules := &recordingRules{out: []Decision{
		{Intervention: "play_audio_cue", Reason: "allowed"},
		{Intervention: "end_game", Reason: "blocked"},
		{Intervention: "grant_shield", Reason: "allowed"},
	}}
	actions := &recordingActions{}
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "rules",
		InterventionsAllowed: []string{"play_audio_cue", "grant_shield"},
	}
	loop := newFlagLoop(snap, rules, actions)

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !actions.called {
		t.Fatalf("actions not applied")
	}
	gotInterventions := make([]string, 0, len(actions.got))
	for _, d := range actions.got {
		gotInterventions = append(gotInterventions, d.Intervention)
	}
	want := []string{"play_audio_cue", "grant_shield"}
	if !reflect.DeepEqual(gotInterventions, want) {
		t.Errorf("dispatched %v, want %v (end_game must be blocked)", gotInterventions, want)
	}
}

func TestOnEvaluate_EmptyAllowListDispatchesNothing(t *testing.T) {
	rules := &recordingRules{out: []Decision{{Intervention: "play_audio_cue"}}}
	actions := &recordingActions{}
	// Enabled but allow-list empty: the safe-default state. No actions dispatch.
	loop := newFlagLoop(flags.Snapshot{Enabled: true, Mode: "rules"}, rules, actions)

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !rules.called {
		t.Errorf("rules should still evaluate when enabled")
	}
	if actions.called {
		t.Errorf("actions applied with empty allow-list, want none dispatched")
	}
}

func TestOnEvaluate_ObjectivesPassedToRules(t *testing.T) {
	rules := &recordingRules{}
	objectives := map[string]float64{"endurance": 0.7, "chaos": 0.3}
	snap := flags.Snapshot{Enabled: true, Mode: "rules", Objectives: objectives}
	loop := newFlagLoop(snap, rules, &recordingActions{})

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !reflect.DeepEqual(rules.gotObjectives, objectives) {
		t.Errorf("rules saw objectives %v, want %v", rules.gotObjectives, objectives)
	}
}

func TestOnEvaluate_LLMModeFallsBackToRules(t *testing.T) {
	rules := &recordingRules{out: []Decision{{Intervention: "play_audio_cue"}}}
	actions := &recordingActions{}
	snap := flags.Snapshot{
		Enabled:              true,
		Mode:                 "llm",
		InterventionsAllowed: []string{"play_audio_cue"},
	}
	loop := newFlagLoop(snap, rules, actions)

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if !rules.called {
		t.Errorf("llm mode should fall back to the rules path until M4")
	}
	if !actions.called {
		t.Errorf("permitted decision from fallback rules should dispatch")
	}
}

func TestNewLoop_NilFlagSourceDefaultsDisabled(t *testing.T) {
	// A nil flag source must default to disabled so it can never dispatch.
	rules := &recordingRules{out: []Decision{{Intervention: "play_audio_cue"}}}
	actions := &recordingActions{}
	loop := NewLoop(nil, nil)
	loop.Rules = rules
	loop.Actions = actions

	loop.OnEvaluate(context.Background(), gamecontext.GameContext{}, testTrigger())

	if rules.called || actions.called {
		t.Errorf("nil flag source should short-circuit (enabled=false default)")
	}
}
