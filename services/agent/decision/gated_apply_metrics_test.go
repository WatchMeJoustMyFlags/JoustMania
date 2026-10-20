package decision

import (
	"context"
	"testing"

	"github.com/joustmania/agent/gamecontext"
)

// gatedSink stands in for actions.GatedActionSink (actions imports decision, so
// the real one cannot be used here): Apply would write, ApplyReported reports
// whether the gate let the write through.
type gatedSink struct {
	gateOn  bool
	applies int
}

func (g *gatedSink) Apply(context.Context, Decision) error {
	g.applies++
	return nil
}

func (g *gatedSink) ApplyReported(ctx context.Context, d Decision) (bool, error) {
	if !g.gateOn {
		return false, nil
	}
	return true, g.Apply(ctx, d)
}

// UC: UC-040 A11
func TestInterventionsApplied_GatedNoopNotCounted(t *testing.T) {
	for _, tc := range []struct {
		name   string
		gateOn bool
		want   int64
	}{
		{"UC-040/A11 gate off: gated no-op is not counted", false, 0},
		{"UC-040/main gate on: the write is counted", true, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			l, _, reader := decisionMetricsLoop(t, permissiveSnap())
			sink := &gatedSink{gateOn: tc.gateOn}
			l.Actions = sink
			l.Rules = fakeRules{out: []Decision{{Intervention: "grant_shield", Reason: "x"}}}

			l.OnEvaluate(context.Background(), gamecontext.GameContext{SessionID: "s"}, testTrigger())

			// The decision itself passed every permission gate either way.
			if got := decisionsByLabels(t, reader)["grant_shield/false/"]; got != 1 {
				t.Errorf("agent_decisions_total{grant_shield,false,} = %d, want 1", got)
			}
			if got := appliedByAction(t, reader)["grant_shield"]; got != tc.want {
				t.Errorf("agent_interventions_applied_total{grant_shield} = %d, want %d", got, tc.want)
			}
		})
	}
}
