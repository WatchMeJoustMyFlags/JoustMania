package main

import (
	"context"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/flagservice"
)

// dynamic_declarer_floor_1907_test.go pins UC-070 BR-008 after #1907 PR 4b: the
// declarer's settings are the experiment_dynamic_* flags, and the compiled
// defaultDynamicDenylist is a FLOOR no flag value can lower (C-034).
//
// The shipped catalog has no experiment+arm row for any built-in denylist key, so
// a test on it would pass whether or not the floor exists. These tests use a fake
// catalog that offers the protected keys shadow_policy and max_shadow_games as
// tunable flags next to the game flags sensitivity and foo.

// protectedCatalog is a flag service whose game catalog makes two built-in
// denylist keys experimentable, so only the declarer's own floor keeps them out.
type protectedCatalog struct{}

func (protectedCatalog) GetSet(context.Context, string) (flagservice.SetView, error) {
	view := func(key string) flagservice.FlagView {
		return flagservice.FlagView{
			Flag:            key,
			Policy:          &flagservice.Policy{Kind: "overlay", Scopes: []string{"stack", experimentScope}},
			BaselineDefault: "a",
			Variants:        []string{"a", "b"},
			VariantValues:   map[string]json.RawMessage{"a": json.RawMessage(`1`), "b": json.RawMessage(`2`)},
		}
	}
	return flagservice.SetView{Flags: []flagservice.FlagView{
		view("shadow_policy"), view("max_shadow_games"), view("sensitivity"), view("foo"),
	}}, nil
}

// rotation returns the declarer's candidate order over the protected catalog after
// configure(cfg).
func rotation(cfg flags.ExperimentConfig) []string {
	d := newDynamicDeclarer(protectedCatalog{}, nil)
	d.configure(cfg)
	return d.candidateOrder(d.readCandidateFlags())
}

// UC: UC-070 BR-008
func TestDynamicDeclarer_BuiltinDenylistIsAFloor(t *testing.T) {
	builtinOnly := []string{"foo", "sensitivity"}
	cases := []struct {
		name string
		cfg  flags.ExperimentConfig
		want []string
	}{
		{"default flags: the built-in list", flags.ExperimentConfig{}, builtinOnly},
		{"a denylist flag cannot replace the built-in list", flags.ExperimentConfig{DynamicDenylist: "x"}, builtinOnly},
		{"a denylist flag adds keys to the built-in list", flags.ExperimentConfig{DynamicDenylist: "foo"}, []string{"sensitivity"}},
		{"blank denylist: the built-in list", flags.ExperimentConfig{DynamicDenylist: "  , ,"}, builtinOnly},
		{"candidates cannot name a built-in key", flags.ExperimentConfig{DynamicCandidates: "shadow_policy"}, []string{}},
		{"candidates are filtered by the floor", flags.ExperimentConfig{DynamicCandidates: "max_shadow_games,foo,shadow_policy"}, []string{"foo"}},
		{"candidates are filtered by the additions", flags.ExperimentConfig{DynamicCandidates: "foo,sensitivity", DynamicDenylist: "foo"}, []string{"sensitivity"}},
	}
	for _, tc := range cases {
		t.Run("UC-070/BR-008 "+tc.name, func(t *testing.T) {
			if got := rotation(tc.cfg); !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("rotation = %v, want %v", got, tc.want)
			}
		})
	}
	t.Run("UC-070/BR-008 candidates naming only built-in keys declare nothing", func(t *testing.T) {
		d := newDynamicDeclarer(protectedCatalog{}, nil)
		d.configure(flags.ExperimentConfig{DynamicCandidates: "shadow_policy,max_shadow_games"})
		if intent, ok := d.Declare(nil); ok {
			t.Fatalf("declared %q from a candidate list of built-in denylist keys", intent.FlagKey)
		}
	})
}

// TestDynamicDeclarer_FlagdDownMeansBuiltinDefaults: when every flag evaluation
// errors (flagd down, a partial sync), the declarer rotates over the catalog minus
// the built-in list, with objective balanced and target 0.
// UC: UC-070 BR-008
func TestDynamicDeclarer_FlagdDownMeansBuiltinDefaults(t *testing.T) {
	down := newTuningEvaluator()
	down.setDown(true)
	cfg := flags.New(down, quietLogger()).Experiment(context.Background(), experimentDefaults())
	d := newDynamicDeclarer(protectedCatalog{}, nil)
	d.configure(cfg)
	if got := d.candidateOrder(d.readCandidateFlags()); !reflect.DeepEqual(got, []string{"foo", "sensitivity"}) {
		t.Fatalf("rotation with flagd down = %v, want [foo sensitivity]", got)
	}
	if d.defaultObjective != decision.ObjectiveBalanced || d.targetN != 0 {
		t.Fatalf("flagd down gave objective %q, target %d; want balanced, 0", d.defaultObjective, d.targetN)
	}
}

// TestDynamicDeclarer_ConfigureResetsDenylist: a denylist flag value applies to one
// configure() only; the next configure with no denylist drops it again, while the
// built-in floor stays (#1907, C-034).
// UC: UC-070 BR-008
func TestDynamicDeclarer_ConfigureResetsDenylist(t *testing.T) {
	d := newDynamicDeclarer(protectedCatalog{}, nil)
	d.configure(flags.ExperimentConfig{DynamicDenylist: "foo"})
	if got := d.candidateOrder(d.readCandidateFlags()); !reflect.DeepEqual(got, []string{"sensitivity"}) {
		t.Fatalf("with denylist foo: rotation = %v, want [sensitivity]", got)
	}
	d.configure(flags.ExperimentConfig{})
	if got := d.candidateOrder(d.readCandidateFlags()); !reflect.DeepEqual(got, []string{"foo", "sensitivity"}) {
		t.Fatalf("after an empty config: rotation = %v, want [foo sensitivity] (foo allowed again, built-in keys still denied)", got)
	}
}

// TestParseCandidates_Rules pins the comma-list parse rules of
// experiment_dynamic_candidates and experiment_dynamic_denylist (#1907).
func TestParseCandidates_Rules(t *testing.T) {
	cases := []struct {
		raw  string
		want []string
	}{
		{"", nil},
		{"   ", nil},
		{" , ,", nil},
		{"a", []string{"a"}},
		{" a , b ", []string{"a", "b"}},
		{"a,,b,", []string{"a", "b"}},
		{"b,a,b,a", []string{"b", "a"}},
		{"A,a", []string{"A", "a"}},
	}
	for _, tc := range cases {
		if got := parseCandidates(tc.raw); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("parseCandidates(%q) = %#v, want %#v", tc.raw, got, tc.want)
		}
	}
}

// TestDeclareDynamic_SettingsAreReadPerTick: the tick path applies this tick's
// experiment_dynamic_* settings before Declare, so an operator overlay takes
// effect on the next tick with no restart (#1907, C-035).
// UC: UC-070 BR-007
func TestDeclareDynamic_SettingsAreReadPerTick(t *testing.T) {
	loop, svc := loopWithRealRegistry(t, 8)
	loop.dynamic = newDynamicDeclarer(svc, nil)
	cfg := dynCfg(5)

	cfg.DynamicCandidates = "nonstop.respawn_seconds"
	loop.declareDynamicIfEnabled(context.Background(), cfg)
	cfg.DynamicCandidates = "fight_club.round_seconds"
	loop.declareDynamicIfEnabled(context.Background(), cfg)

	active := loop.registry.ActiveFlags()
	_, first := active["nonstop.respawn_seconds"]
	_, second := active["fight_club.round_seconds"]
	if !first || !second || len(active) != 2 {
		t.Fatalf("active = %v, want nonstop.respawn_seconds then fight_club.round_seconds", active)
	}
}
