package flags

import (
	"context"
	"errors"
	"testing"
)

// probe_test.go covers decision.probe_decisions (#1810, C-035, FR-151): the flag
// wins when it resolves, the deprecated AGENT_PROBE_DECISIONS value is only the
// fallback when it cannot, the default is off, and Evaluate reads it every call.

// UC: UC-040 A23
func TestProbeDecisions_Resolution(t *testing.T) {
	down := errors.New("PROVIDER_NOT_READY")
	cases := []struct {
		name        string
		ev          stubEvaluator
		envFallback bool
		want        bool
	}{
		{"UC-040/A23 flag on wins over env off", stubEvaluator{booleans: map[string]bool{keyProbeDecisions: true}}, false, true},
		{"UC-040/A23 flag off wins over env on", stubEvaluator{booleans: map[string]bool{keyProbeDecisions: false}}, true, false},
		{"UC-040/A23 flagd down falls back to env on", stubEvaluator{errs: map[string]error{keyProbeDecisions: down}}, true, true},
		{"UC-040/A23 flagd down and no env is off", stubEvaluator{errs: map[string]error{keyProbeDecisions: down}}, false, false},
		{"UC-040/A23 default is off", stubEvaluator{}, false, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			f := New(c.ev, nil)
			f.SetProbeDefault(c.envFallback)
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
