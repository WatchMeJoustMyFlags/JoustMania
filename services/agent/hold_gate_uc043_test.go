package main

import (
	"context"
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"sort"
	"testing"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/experiment"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
	"github.com/joustmania/agent/flagservice/fake"
	"github.com/joustmania/agent/gamecontext"
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
			if v, ok := got[experiment.HoldActivationKey]; ok && v != any(experiment.HoldValue) {
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
	t.Run("UC-043/A17 CI baseline instant: shield.activation still toward hold", func(t *testing.T) {
		// The CI baseline default is instant, whose next variant in sort order is
		// trigger: the declarer must still try hold.
		svc := gameServiceFrom(t, filepath.Join("..", "flagd", "ci", "game.json"))
		got := declaredKeys(t, svc, policyGate(true))
		if v, ok := got[experiment.HoldActivationKey]; !ok || v != any(experiment.HoldValue) || len(got) != 1 {
			t.Fatalf("declared %v, want only shield.activation toward hold", got)
		}
	})
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

// UC: UC-043 A17
func TestGameAttrsWiring(t *testing.T) {
	t.Run("UC-043/A17 gameAttrsFrom returns a shadow arm game's kind, experiment and arm", func(t *testing.T) {
		mux := gamecontext.NewMultiplexer(func(string) *gamecontext.Store {
			return gamecontext.NewStore(time.Hour, time.Hour, time.Now)
		})
		md := gameActiveMetric("g-arm")
		dp := md.ResourceMetrics().At(0).ScopeMetrics().At(0).Metrics().At(0).Gauge().DataPoints().At(0)
		dp.Attributes().PutStr("game_kind", "shadow")
		dp.Attributes().PutStr("experiment_id", "exp_1")
		dp.Attributes().PutStr("arm", "experimental")
		mux.ApplyMetrics(md)
		got := gameAttrsFrom(mux)("g-arm")
		want := flags.GameAttrs{GameKind: "shadow", ExperimentID: "exp_1", Arm: "experimental"}
		if got != want {
			t.Fatalf("attrs = %+v, want %+v", got, want)
		}
		if got := gameAttrsFrom(mux)("unknown"); got != (flags.GameAttrs{}) {
			t.Fatalf("unknown game attrs = %+v, want zero (real)", got)
		}
	})
	t.Run("UC-043/A17 main wires the Multiplexer lookup into the agent flags", func(t *testing.T) {
		file, err := parser.ParseFile(token.NewFileSet(), "main.go", nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		found := false
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok || len(call.Args) != 1 {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "SetGameAttrs" {
				return true
			}
			if inner, ok := call.Args[0].(*ast.CallExpr); ok {
				if id, ok := inner.Fun.(*ast.Ident); ok && id.Name == "gameAttrsFrom" {
					found = true
				}
			}
			return true
		})
		if !found {
			t.Fatal("main.go does not call SetGameAttrs(gameAttrsFrom(mux)): the agent's shield reads would miss the arm (opposer M2)")
		}
	})
}
