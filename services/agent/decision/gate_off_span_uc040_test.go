package decision

import (
	"context"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-040 A11
// A write the interventions_enabled gate drops is recorded as not applied on the
// agent.decision and agent.action spans, with the reason; a written one says
// applied=true (#2093).
func TestGateOff_SpansRecordNotApplied(t *testing.T) {
	for _, tc := range []struct {
		name        string
		gateOn      bool
		wantApplied bool
		wantReason  string
	}{
		{"UC-040/A11 gate off: not applied, write_gate_off", false, false, NotAppliedWriteGateOff},
		{"UC-040/main gate on: applied", true, true, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, sr, _ := newTestLoop(t)
			l.Actions = &gatedSink{gateOn: tc.gateOn}
			l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}

			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

			for _, name := range []string{SpanDecision, SpanAction} {
				spans := spansByName(sr.Ended(), name)
				if len(spans) != 1 {
					t.Fatalf("%s spans = %d, want 1", name, len(spans))
				}
				v, ok := attrValue(spans[0], AttrDecisionApplied)
				if !ok || v.AsBool() != tc.wantApplied {
					t.Errorf("%s %s = %v (present %v), want %v", name, AttrDecisionApplied, v.Emit(), ok, tc.wantApplied)
				}
				r, has := attrValue(spans[0], AttrDecisionNotAppliedReason)
				if has != (tc.wantReason != "") || r.AsString() != tc.wantReason {
					t.Errorf("%s %s = %q (present %v), want %q", name, AttrDecisionNotAppliedReason, r.AsString(), has, tc.wantReason)
				}
			}
		})
	}
}

// UC: UC-035 5
// agent.flag_state carries the live write gate, so a re-emit shows it flipping (#2093).
func TestFlagState_CarriesInterventionsEnabled(t *testing.T) {
	l, sr, fl := newTestLoop(t)
	c := modeCtx("game_fs2093", "FFA")
	l.OnEvaluate(context.Background(), c, testTrigger())
	fl.snap.InterventionsEnabled = true
	l.OnEvaluate(context.Background(), c, testTrigger())

	spans := flagStateSpans(sr)
	if len(spans) != 2 {
		t.Fatalf("agent.flag_state = %d, want 2 (the gate flip is a change)", len(spans))
	}
	for i, want := range []bool{false, true} {
		v, ok := attrValue(spans[i], AttrInterventionsEnabled)
		if !ok || v.AsBool() != want {
			t.Errorf("span %d %s = %v (present %v), want %v", i, AttrInterventionsEnabled, v.Emit(), ok, want)
		}
	}
}
