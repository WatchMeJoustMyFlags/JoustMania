package main

import (
	"context"
	"path/filepath"
	"sort"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
)

var holdSubParams = []string{
	"shield.energy_max_seconds", "shield.energy_budget_seconds", "shield.energy_initial_seconds",
	"shield.recharge_per_second", "shield.min_burst_seconds",
	"shield.hold_rumble_intensity", "shield.empty_pulse_ms",
}

// holdService is the shipped stack game set (shield.activation defaults to trigger)
// with an optional stack and real overlay on shield.activation.
func holdService(t *testing.T, stack, real string) *fake.Service {
	t.Helper()
	svc := gameServiceFrom(t, filepath.Join("..", "flagd", "game.json"))
	put := func(variant string, scope flagservice.Scope) {
		if variant == "" {
			return
		}
		if _, err := svc.Put(context.Background(), experiment.GameFlagSet, experiment.HoldActivationKey,
			flagservice.Write{Variant: variant, Scope: scope}); err != nil {
			t.Fatalf("put %s %+v: %v", variant, scope, err)
		}
	}
	put(stack, flagservice.Scope{})
	put(real, flagservice.Scope{GameKind: "real"})
	return svc
}

func policyGate(ok bool) *holdGate {
	g := &holdGate{}
	g.setPolicy(func(context.Context) bool { return ok })
	return g
}

// declaredKeys runs the declarer over the shield keys until it stops and returns
// each declared key with its experimental value.
func declaredKeys(t *testing.T, svc *fake.Service, gate *holdGate) map[string]any {
	t.Helper()
	d := &dynamicDeclarer{gameFlags: svc, defaultObjective: decision.ObjectiveBalanced, hold: gate}
	d.candidates = append([]string{experiment.HoldActivationKey}, holdSubParams...)
	active := activeFlags{}
	out := map[string]any{}
	for range len(d.candidates) + 1 {
		in, ok := d.Declare(active)
		if !ok {
			break
		}
		out[in.FlagKey] = in.ExperimentalValue
		active[in.FlagKey] = struct{}{}
	}
	return out
}

func keysOf[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// UC: UC-043 A17
func TestHoldGate_DeclarerAndVocab(t *testing.T) {
	cases := []struct {
		name        string
		stack, real string
		policy      bool
		want        []string
	}{
		{"UC-043/A17 trigger default: shield.activation only, toward hold", "", "", true, []string{experiment.HoldActivationKey}},
		{"UC-043/A17 archetypes without a trigger policy: no hold key", "", "", false, nil},
		{"UC-043/A17 real overlay hold: the sub-parameters only", "", "hold", true, holdSubParams},
		{"UC-043/A17 stage overlay hold: the sub-parameters only", "hold", "", true, holdSubParams},
		{"UC-043/A17 stage trigger outranks a real hold: shield.activation only", "trigger", "hold", true, []string{experiment.HoldActivationKey}},
		{"UC-043/A17 hold resolved but no trigger policy: no hold key", "hold", "", false, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			svc := holdService(t, tc.stack, tc.real)
			want := append([]string(nil), tc.want...)
			sort.Strings(want)

			got := declaredKeys(t, svc, policyGate(tc.policy))
			if keys := keysOf(got); !equalStrings(keys, want) {
				t.Fatalf("declared %v, want %v", keys, want)
			}
			if v, ok := got[experiment.HoldActivationKey]; ok && v != experiment.HoldValue {
				t.Fatalf("shield.activation declared toward %v, want hold", v)
			}

			var vocabHold []string
			for k := range gameFlagVocab(svc, policyGate(tc.policy))() {
				if experiment.IsHoldKey(k) {
					vocabHold = append(vocabHold, k)
				}
			}
			sort.Strings(vocabHold)
			if !equalStrings(vocabHold, want) {
				t.Fatalf("proposer vocabulary hold keys %v, want %v", vocabHold, want)
			}
		})
	}
	t.Run("UC-043/A17 no policy set yet: fail closed", func(t *testing.T) {
		svc := holdService(t, "hold", "")
		if got := declaredKeys(t, svc, &holdGate{}); len(got) != 0 {
			t.Fatalf("declared %v with no policy", keysOf(got))
		}
	})
}

// UC: UC-043 A17
func TestHoldGate_RegistryReasons(t *testing.T) {
	ctx := context.Background()
	cases := []struct {
		name, stack, real, key, want string
	}{
		{"UC-043/A17 sub-parameter under trigger", "", "", "shield.energy_budget_seconds", experiment.DiscardHoldNotRealDefault},
		{"UC-043/A17 sub-parameter under hold", "", "hold", "shield.energy_budget_seconds", ""},
		{"UC-043/A17 activation under trigger", "", "", experiment.HoldActivationKey, ""},
		{"UC-043/A17 activation already hold", "hold", "", experiment.HoldActivationKey, experiment.DiscardHoldAlreadyDefault},
		{"UC-043/A17 a non-hold key is never gated", "", "", "windows", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := registryHoldGate(holdService(t, tc.stack, tc.real))(ctx, tc.key)
			if err != nil || got != tc.want {
				t.Fatalf("reason %q err %v, want %q", got, err, tc.want)
			}
		})
	}
	t.Run("UC-043/A17 a read error is an error, not a reason", func(t *testing.T) {
		if _, err := registryHoldGate(setReader{err: context.DeadlineExceeded})(ctx, "shield.energy_budget_seconds"); err == nil {
			t.Fatal("want the read error")
		}
	})
	t.Run("UC-043/A17 an unreadable activation fails closed", func(t *testing.T) {
		if got := holdValueReason(nil, "shield.min_burst_seconds"); got != experiment.DiscardHoldNotRealDefault {
			t.Fatalf("reason %q", got)
		}
	})
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
