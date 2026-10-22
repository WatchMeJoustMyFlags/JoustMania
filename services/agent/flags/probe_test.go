package flags

import (
	"context"
	"errors"
	"testing"
)

// probe_test.go covers decision.probe_decisions (#1810, C-035): the flag decides when
// it resolves, the probe is off when it cannot (the AGENT_PROBE_DECISIONS env is
// removed, #1907), and Evaluate reads it every call.

// UC: UC-040 A23
func TestProbeDecisions_Resolution(t *testing.T) {
	down := errors.New("PROVIDER_NOT_READY")
	cases := []struct {
		name string
		ev   stubEvaluator
		want bool
	}{
		{"UC-040/A23 flag on", stubEvaluator{booleans: map[string]bool{keyProbeDecisions: true}}, true},
		{"UC-040/A23 flag off", stubEvaluator{booleans: map[string]bool{keyProbeDecisions: false}}, false},
		{"UC-040/A23 flagd down is off", stubEvaluator{errs: map[string]error{keyProbeDecisions: down}}, false},
		{"UC-040/A23 default is off", stubEvaluator{}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := New(c.ev, nil)
			if got := f.Evaluate(context.Background()).ProbeDecisions; got != c.want {
				t.Fatalf("Snapshot.ProbeDecisions = %v, want %v", got, c.want)
			}
		})
	}
}

// UC: UC-040 A23
func TestProbeDecisions_ReadEveryEvaluate(t *testing.T) {
	t.Run("UC-040/A23 a flip is visible on the next Evaluate", func(t *testing.T) {
		ev := stubEvaluator{booleans: map[string]bool{keyProbeDecisions: false}}
		f := New(ev, nil)
		ctx := context.Background()
		if f.Evaluate(ctx).ProbeDecisions {
			t.Fatal("probe on before the flip")
		}
		ev.booleans[keyProbeDecisions] = true
		if !f.Evaluate(ctx).ProbeDecisions {
			t.Fatal("the flip did not reach the next Evaluate (flag read at startup?)")
		}
		ev.booleans[keyProbeDecisions] = false
		if f.Evaluate(ctx).ProbeDecisions {
			t.Fatal("the flip back did not reach the next Evaluate")
		}
	})
}
