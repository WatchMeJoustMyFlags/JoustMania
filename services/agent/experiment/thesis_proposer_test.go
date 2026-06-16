package experiment

import (
	"strings"
	"testing"
)

// fakeSelector is a FlagSelector that returns a fixed Intent (or ok=false), and
// records whether it was called — so a test can prove the proposer delegates flag
// selection and consumes a suggestion only on a successful select.
type fakeSelector struct {
	intent Intent
	ok     bool
	calls  int
}

func (f *fakeSelector) sel(active map[string]struct{}) (Intent, bool) {
	f.calls++
	return f.intent, f.ok
}

// baseSelected is the partially-filled Intent a selector hands back (flag + value +
// a default objective + targetN) — the proposer keeps these and overwrites the
// thesis/objective.
func baseSelected() Intent {
	return Intent{
		Hypothesis:        "selector default (should be overwritten)",
		FlagKey:           "death_grace_period_seconds",
		ExperimentalValue: 0.5,
		Objective:         "balanced",
		TargetNPerArm:     20,
	}
}

// TestRecord_DropsEmptySuggestion: a suggestion with no reason and no intervention
// type carries nothing to seed a thesis from, so it is dropped.
func TestRecord_DropsEmptySuggestion(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{})
	tp.Record(RetroSuggestion{Emphasis: "weight_up"}) // emphasis-only is dropped (no reason/type)
	if got := tp.Backlog(); got != 0 {
		t.Fatalf("backlog = %d, want 0 (empty suggestions dropped)", got)
	}
}

// TestRecord_BoundedRing: the backlog never grows past maxBacklog; the most-recent
// suggestions are kept (oldest evicted).
func TestRecord_BoundedRing(t *testing.T) {
	tp := NewThesisProposer(2)
	tp.Record(RetroSuggestion{Reason: "first"})
	tp.Record(RetroSuggestion{Reason: "second"})
	tp.Record(RetroSuggestion{Reason: "third"})
	if got := tp.Backlog(); got != 2 {
		t.Fatalf("backlog = %d, want 2 (bounded ring)", got)
	}
	// The oldest ("first") must have been evicted: the next Propose drains "second".
	sel := &fakeSelector{intent: baseSelected(), ok: true}
	intent, ok := tp.Propose(sel.sel, nil)
	if !ok {
		t.Fatal("propose: want ok")
	}
	if !strings.Contains(intent.Hypothesis, "second") {
		t.Errorf("thesis = %q, want it seeded from the oldest RETAINED suggestion (second)", intent.Hypothesis)
	}
}

// TestPropose_NoBacklog_NoOp: with nothing recorded, Propose returns ok=false and
// never calls the selector (no wasted flag selection).
func TestPropose_NoBacklog_NoOp(t *testing.T) {
	tp := NewThesisProposer(0)
	sel := &fakeSelector{intent: baseSelected(), ok: true}
	if _, ok := tp.Propose(sel.sel, nil); ok {
		t.Fatal("propose with empty backlog must return ok=false")
	}
	if sel.calls != 0 {
		t.Errorf("selector called %d times, want 0 (no backlog ⇒ no selection)", sel.calls)
	}
}

// TestPropose_SeedsThesisFromSuggestion: a recorded suggestion is turned into an
// Intent whose thesis embeds the retro's intervention type, emphasis, and reason —
// the provenance that closes the game→retro→thesis loop. The selector's flag/value
// are kept; the objective is biased from the emphasis.
func TestPropose_SeedsThesisFromSuggestion(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{
		InterventionType: "adjust_global_difficulty",
		Emphasis:         "weight_up",
		Reason:           "the room faded after the first elimination",
	})
	sel := &fakeSelector{intent: baseSelected(), ok: true}

	intent, ok := tp.Propose(sel.sel, nil)
	if !ok {
		t.Fatal("propose: want ok")
	}
	// Flag/value preserved from the selector.
	if intent.FlagKey != "death_grace_period_seconds" || intent.ExperimentalValue != 0.5 {
		t.Errorf("flag/value = %s/%v, want the selector's choice preserved", intent.FlagKey, intent.ExperimentalValue)
	}
	// Thesis carries the retro provenance.
	for _, want := range []string{"adjust_global_difficulty", "weight_up", "the room faded", "retro-seeded"} {
		if !strings.Contains(intent.Hypothesis, want) {
			t.Errorf("thesis %q missing %q", intent.Hypothesis, want)
		}
	}
	// weight_up biases the objective to accelerate (act more).
	if intent.Objective != "accelerate" {
		t.Errorf("objective = %q, want accelerate (weight_up bias)", intent.Objective)
	}
	// The suggestion was consumed.
	if got := tp.Backlog(); got != 0 {
		t.Errorf("backlog = %d, want 0 (consumed on success)", got)
	}
}

// TestPropose_KeepsSuggestionWhenNothingDeclarable: when the selector finds no
// declarable flag (full surface), the suggestion is NOT consumed — it waits for a
// later tick.
func TestPropose_KeepsSuggestionWhenNothingDeclarable(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{Reason: "keep me"})
	sel := &fakeSelector{ok: false}

	if _, ok := tp.Propose(sel.sel, nil); ok {
		t.Fatal("propose must return ok=false when the selector finds nothing declarable")
	}
	if got := tp.Backlog(); got != 1 {
		t.Errorf("backlog = %d, want 1 (suggestion retained, not silently dropped)", got)
	}
}

// TestPropose_NilSelector_NoOp: a nil selector is a safe no-op (defends the wiring
// seam — the loop only invokes Propose with the dynamic selector wired).
func TestPropose_NilSelector_NoOp(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{Reason: "x"})
	if _, ok := tp.Propose(nil, nil); ok {
		t.Fatal("propose with nil selector must return ok=false")
	}
	if got := tp.Backlog(); got != 1 {
		t.Errorf("backlog = %d, want 1 (nothing consumed on nil selector)", got)
	}
}

// TestObjectiveFromEmphasis covers the emphasis→objective bias table, including the
// unrecognized case that leaves the selector's objective untouched.
func TestObjectiveFromEmphasis(t *testing.T) {
	cases := []struct {
		emphasis string
		want     string
		ok       bool
	}{
		{"weight_up", "accelerate", true},
		{"enable", "accelerate", true},
		{"weight_down", "endurance", true},
		{"disable", "endurance", true},
		{"WEIGHT_UP", "accelerate", true}, // case-insensitive
		{"", "", false},
		{"sideways", "", false},
	}
	for _, c := range cases {
		got, ok := objectiveFromEmphasis(c.emphasis)
		if got != c.want || ok != c.ok {
			t.Errorf("objectiveFromEmphasis(%q) = (%q,%v), want (%q,%v)", c.emphasis, got, ok, c.want, c.ok)
		}
	}
}

// TestPropose_UnknownEmphasis_KeepsSelectorObjective: an emphasis with no direction
// bias leaves the selector's default objective intact.
func TestPropose_UnknownEmphasis_KeepsSelectorObjective(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{InterventionType: "send_controller_effect", Reason: "flavor"})
	sel := &fakeSelector{intent: baseSelected(), ok: true}
	intent, ok := tp.Propose(sel.sel, nil)
	if !ok {
		t.Fatal("propose: want ok")
	}
	if intent.Objective != "balanced" {
		t.Errorf("objective = %q, want balanced (selector default kept when emphasis gives no bias)", intent.Objective)
	}
}

// TestPropose_FIFOOrder: suggestions are drained oldest-first across calls.
func TestPropose_FIFOOrder(t *testing.T) {
	tp := NewThesisProposer(0)
	tp.Record(RetroSuggestion{Reason: "alpha"})
	tp.Record(RetroSuggestion{Reason: "beta"})
	sel := &fakeSelector{intent: baseSelected(), ok: true}

	first, _ := tp.Propose(sel.sel, nil)
	second, _ := tp.Propose(sel.sel, nil)
	if !strings.Contains(first.Hypothesis, "alpha") {
		t.Errorf("first thesis = %q, want oldest (alpha) first", first.Hypothesis)
	}
	if !strings.Contains(second.Hypothesis, "beta") {
		t.Errorf("second thesis = %q, want beta next", second.Hypothesis)
	}
}
