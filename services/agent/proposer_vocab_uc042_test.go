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
		{Flag: "sensitivity", Policy: &flagservice.Policy{Scopes: []string{"stack", "experiment+arm"}}},
		{Flag: "operator_only", Policy: &flagservice.Policy{Scopes: []string{"stack"}}},
		{Flag: "no_policy"},
	}}}
	got := gameFlagVocab(r, nil)()
	if _, ok := got["sensitivity"]; !ok || len(got) != 1 {
		t.Fatalf("vocab = %v, want exactly {sensitivity}", got)
	}

	if got := gameFlagVocab(setReader{err: errors.New("down")}, nil)(); len(got) != 0 {
		t.Fatalf("vocab on read error = %v, want empty (fail closed)", got)
	}
}
