package main

import (
	"context"
	"errors"
	"testing"

	"github.com/joustmania/agent/flagservice"
)

type setReader struct {
	set flagservice.SetView
	err error
}

func (r setReader) GetSet(context.Context, string) (flagservice.SetView, error) {
	return r.set, r.err
}

// The production proposer vocabulary is the experiment catalog only: game flags
// whose registry row allows an experiment-arm write. Flags without that scope
// (or without a policy) are not proposable; an unreachable flag service yields
// an empty set (fail closed).
// UC: UC-042 main
func TestGameFlagVocab_ExperimentCatalogOnly(t *testing.T) {
	r := setReader{set: flagservice.SetView{Flags: []flagservice.FlagView{
		{Flag: "sensitivity", Policy: &flagservice.Policy{Scopes: []string{"stack", "experiment+arm"}, Description: "movement a player survives"}},
		{Flag: "operator_only", Policy: &flagservice.Policy{Scopes: []string{"stack"}}},
		{Flag: "no_policy"},
	}}}
	got := gameFlagVocab(r, nil)()
	if _, ok := got["sensitivity"]; !ok || len(got) != 1 {
		t.Fatalf("vocab = %v, want exactly {sensitivity}", got)
	}
	// The entry keeps the flag-service view, so the prompt can show the
	// description (UC-042 BR-008, #2345).
	if d := got["sensitivity"].Policy.Description; d != "movement a player survives" {
		t.Fatalf("vocab entry description = %q, want the served one", d)
	}

	if got := gameFlagVocab(setReader{err: errors.New("down")}, nil)(); len(got) != 0 {
		t.Fatalf("vocab on read error = %v, want empty (fail closed)", got)
	}
}

// A protected key on the built-in dynamic denylist never enters the proposer
// vocabulary, even when a catalog row offers it with experiment+arm scope: the
// vocabulary applies the same floor as the declarer (UC-070 BR-008, C-034, #1961).
// UC: UC-070 BR-008
func TestGameFlagVocab_BuiltinDenylistFloor(t *testing.T) {
	got := gameFlagVocab(protectedCatalog{}, nil)()
	for _, k := range []string{"shadow_policy", "max_shadow_games"} {
		if _, ok := got[k]; ok {
			t.Errorf("vocab offers denylisted key %q: %v", k, got)
		}
	}
	for _, k := range []string{"sensitivity", "foo"} {
		if _, ok := got[k]; !ok {
			t.Errorf("vocab misses game flag %q: %v", k, got)
		}
	}
}
