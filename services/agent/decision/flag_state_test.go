package decision

import (
	"context"
	"sort"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// flag_state_test.go covers agent.flag_state (#2059, docs/specs/telemetry.md §4.1):
// one span per game partition on the first evaluated cycle and on every change of the
// effective snapshot, before the kill switch, in the game trace, with the
// feature_flag.evaluation events of the evaluation that produced it.

// eventingFlags is a flag source that, like the OpenFeature traces hook, adds one
// feature_flag.evaluation event per evaluated flag to the span in ctx.
type eventingFlags struct{ snap flags.Snapshot }

func (f *eventingFlags) Evaluate(ctx context.Context) flags.Snapshot {
	span := trace.SpanFromContext(ctx)
	for _, key := range []string{"enabled", "policy.battery_threshold"} {
		span.AddEvent("feature_flag.evaluation", trace.WithAttributes(attribute.String("feature_flag.key", key)))
	}
	return f.snap
}

func flagStateSpans(sr interface {
	Ended() []sdktrace.ReadOnlySpan
}) []sdktrace.ReadOnlySpan {
	return spansByName(sr.Ended(), SpanFlagState)
}

func modeCtx(gameID, mode string) gamecontext.GameContext {
	c := gamecontext.GameContext{SessionID: gameID, GameKind: gamecontext.GameKindReal}
	c.Session.GameMode = &mode
	return c
}

// UC: UC-035 5
func TestFlagState_EmittedOnFirstCycleAndOnChangeOnly(t *testing.T) {
	l, sr, fl := newTestLoop(t)
	c := modeCtx("game_fs01", "FFA")

	for i := 0; i < 3; i++ { // idle cycles with an unchanged snapshot
		l.OnEvaluate(context.Background(), c, testTrigger())
	}
	got := flagStateSpans(sr)
	if len(got) != 1 {
		t.Fatalf("UC-035/5 agent.flag_state after 3 identical cycles = %d, want 1", len(got))
	}
	if v, _ := attrValue(got[0], AttrFlagStateTrigger); v.AsString() != FlagStateTriggerGameStart {
		t.Errorf("first trigger = %q, want %q", v.AsString(), FlagStateTriggerGameStart)
	}

	fl.snap.Policy.BatteryThreshold = 3 // a flag change
	l.OnEvaluate(context.Background(), c, testTrigger())
	l.OnEvaluate(context.Background(), c, testTrigger())
	got = flagStateSpans(sr)
	if len(got) != 2 {
		t.Fatalf("UC-035/5 agent.flag_state after one change = %d, want 2", len(got))
	}
	if v, _ := attrValue(got[1], AttrFlagStateTrigger); v.AsString() != FlagStateTriggerChanged {
		t.Errorf("change trigger = %q, want %q", v.AsString(), FlagStateTriggerChanged)
	}
	if v, _ := attrValue(got[1], AttrPolicyBatteryThreshold); v.AsInt64() != 3 {
		t.Errorf("policy.battery_threshold = %d, want 3", v.AsInt64())
	}

	// A mode learned later is a change of the partition's effective state too.
	l.OnEvaluate(context.Background(), modeCtx("game_fs01", "Teams"), testTrigger())
	if n := len(flagStateSpans(sr)); n != 3 {
		t.Fatalf("agent.flag_state after a mode change = %d, want 3", n)
	}
}

// UC: UC-035 5
func TestFlagState_AttributeSet(t *testing.T) {
	l, sr, fl := newTestLoop(t)
	fl.snap.Objectives = map[string]float64{"endurance": 0.7, "balanced": 0.3}
	fl.snap.InterventionsAllowed = []string{"grant_shield", "noop"}
	fl.snap.Policy = flags.Policy{BatteryThreshold: 2, MaxInterventionsPerMinute: 6}
	fl.snap.Decision = flags.DecisionBackend{Backend: flags.DecisionBackendJev, ActConfidenceThreshold: 0.6}
	l.OnEvaluate(context.Background(), modeCtx("game_fs02", "FFA"), testTrigger())

	spans := flagStateSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("agent.flag_state = %d, want 1", len(spans))
	}
	want := map[string]attribute.Value{
		AttrGameID:                 attribute.StringValue("game_fs02"),
		AttrSessionID:              attribute.StringValue("game_fs02"),
		AttrGameKind:               attribute.StringValue(gamecontext.GameKindReal),
		AttrGameMode:               attribute.StringValue("FFA"),
		AttrFlagStateTrigger:       attribute.StringValue(FlagStateTriggerGameStart),
		AttrEnabled:                attribute.BoolValue(true),
		AttrObjectives:             attribute.StringValue("balanced=0.3,endurance=0.7"),
		AttrInterventionsAllowed:   attribute.StringValue("grant_shield,noop"),
		AttrPolicyBatteryThreshold: attribute.IntValue(2),
		AttrPolicyMaxPerMinute:     attribute.IntValue(6),
		AttrDecideBackendRequested: attribute.StringValue(flags.DecisionBackendJev),
		AttrDecideActThreshold:     attribute.Float64Value(0.6),
	}
	var gotKeys []string
	for _, kv := range spans[0].Attributes() {
		gotKeys = append(gotKeys, string(kv.Key))
		w, ok := want[string(kv.Key)]
		if !ok {
			t.Errorf("unexpected attribute %s", kv.Key)
			continue
		}
		if kv.Value != w {
			t.Errorf("%s = %v, want %v", kv.Key, kv.Value.Emit(), w.Emit())
		}
	}
	sort.Strings(gotKeys)
	if len(gotKeys) != len(want) {
		t.Errorf("attributes = %v, want the %d keys of the spec row", gotKeys, len(want))
	}
	// game.name is the coordinator's root-span marker the agent's ingest keys on.
	if _, ok := attrValue(spans[0], "game.name"); ok {
		t.Error("agent.flag_state must never carry game.name")
	}
}

// UC: UC-045 BR-001
func TestFlagState_EmittedWhenKillSwitchOff(t *testing.T) {
	l, sr, fl := newTestLoop(t)
	fl.snap.Enabled = false
	l.OnEvaluate(context.Background(), modeCtx("game_fs03", "FFA"), testTrigger())

	spans := flagStateSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("UC-045/BR-001 agent.flag_state with the kill switch off = %d, want 1", len(spans))
	}
	if v, _ := attrValue(spans[0], AttrEnabled); v.AsBool() {
		t.Error("agent.enabled = true, want false")
	}
}

// UC: UC-040 BR-013
func TestFlagState_ParentIsGameCycle(t *testing.T) {
	cases := []struct {
		name       string
		ctx        gamecontext.GameContext
		wantParent string
	}{
		{"UC-040/BR-013 game_cycle known: child of game_cycle", gameCycleCtx("game_fs04"), hubGameCycleSpanID},
		{"UC-040/BR-013 no game ids: own root", gamecontext.GameContext{SessionID: "game_fs05"}, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			l, sr, _ := newTestLoop(t)
			l.OnEvaluate(context.Background(), tc.ctx, testTrigger())
			spans := flagStateSpans(sr)
			if len(spans) != 1 {
				t.Fatalf("agent.flag_state = %d, want 1", len(spans))
			}
			parent := spans[0].Parent()
			if tc.wantParent == "" {
				if parent.IsValid() {
					t.Fatalf("parent = %s, want own root", parent.SpanID())
				}
				return
			}
			if !parent.IsRemote() || parent.SpanID().String() != tc.wantParent {
				t.Fatalf("parent = %s (remote %v), want remote %s", parent.SpanID(), parent.IsRemote(), tc.wantParent)
			}
			if parent.TraceID().String() != tc.ctx.GameTraceID {
				t.Errorf("trace = %s, want the game trace %s", parent.TraceID(), tc.ctx.GameTraceID)
			}
		})
	}
}

// UC: UC-035 5
func TestFlagState_CarriesTheFlagEvaluationEvents(t *testing.T) {
	l, sr, _ := newTestLoop(t)
	l.Flags = &eventingFlags{snap: flags.Snapshot{Enabled: true}}
	c := modeCtx("game_fs06", "FFA")
	l.OnEvaluate(context.Background(), c, testTrigger())
	l.OnEvaluate(context.Background(), c, testTrigger()) // unchanged: no span, no events

	spans := flagStateSpans(sr)
	if len(spans) != 1 {
		t.Fatalf("agent.flag_state = %d, want 1", len(spans))
	}
	var keys []string
	for _, ev := range spans[0].Events() {
		if ev.Name != "feature_flag.evaluation" {
			continue
		}
		for _, kv := range ev.Attributes {
			if kv.Key == "feature_flag.key" {
				keys = append(keys, kv.Value.AsString())
			}
		}
	}
	if len(keys) != 2 || keys[0] != "enabled" || keys[1] != "policy.battery_threshold" {
		t.Fatalf("feature_flag.evaluation keys = %v, want [enabled policy.battery_threshold]", keys)
	}
}

// A span already in the caller's context still receives the evaluation events.
// UC: UC-035 5
func TestFlagState_EventsStillReachTheCallersSpan(t *testing.T) {
	l, sr, _ := newTestLoop(t)
	l.Flags = &eventingFlags{snap: flags.Snapshot{Enabled: true}}
	ctx, outer := l.Tracer.Start(context.Background(), "outer")
	l.OnEvaluate(ctx, modeCtx("game_fs07", "FFA"), testTrigger())
	outer.End()
	o := spansByName(sr.Ended(), "outer")
	if len(o) != 1 || len(o[0].Events()) != 2 {
		t.Fatalf("outer span events = %v, want the 2 evaluation events", o)
	}
}

// The partition's first cycle can run before the game_trace_correlation ids arrive
// (a trace Export opens it first): that span is its own root, and the span is emitted
// again, under game_cycle, once the parent is known, even with an unchanged snapshot.
// UC: UC-040 BR-013
func TestFlagState_ReEmittedOnceTheGameParentIsKnown(t *testing.T) {
	l, sr, _ := newTestLoop(t)
	withParent := gameCycleCtx("game_fs08")
	noParent := withParent
	noParent.GameTraceID, noParent.GameTraceSpanID, noParent.GameplayPhaseSpanID, noParent.GameCycleSpanID = "", "", "", ""

	l.OnEvaluate(context.Background(), noParent, testTrigger())
	l.OnEvaluate(context.Background(), withParent, testTrigger())
	l.OnEvaluate(context.Background(), withParent, testTrigger()) // unchanged: nothing more

	spans := flagStateSpans(sr)
	if len(spans) != 2 {
		t.Fatalf("UC-040/BR-013 agent.flag_state = %d, want 2 (own root, then under game_cycle)", len(spans))
	}
	if spans[0].Parent().IsValid() {
		t.Errorf("first span parent = %s, want its own root", spans[0].Parent().SpanID())
	}
	parent := spans[1].Parent()
	if !parent.IsRemote() || parent.SpanID().String() != hubGameCycleSpanID || parent.TraceID().String() != withParent.GameTraceID {
		t.Fatalf("second span parent = %s/%s (remote %v), want remote game_cycle %s", parent.TraceID(), parent.SpanID(), parent.IsRemote(), hubGameCycleSpanID)
	}
	if v, _ := attrValue(spans[1], AttrFlagStateTrigger); v.AsString() != FlagStateTriggerChanged {
		t.Errorf("second trigger = %q, want %q", v.AsString(), FlagStateTriggerChanged)
	}
}
