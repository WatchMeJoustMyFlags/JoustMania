package prediction

import (
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// UC: UC-042 A12 BR-009
// The summary and the retro read a game's results through SummaryOutcomes: only
// after the game ended, with the source, target, acted flag and registration time.
func TestBook_SummaryOutcomes(t *testing.T) {
	h := newHarness(t)
	c := game("P1", "P2")
	h.register(pred(decide.TemplateSurvives, 15, "P1"), c, false)
	if got := h.b.SummaryOutcomes("g1"); got != nil {
		t.Fatalf("a running game has no finalized outcomes, got %+v", got)
	}
	h.at(18 * time.Second)
	h.setLive(&c)
	h.fire()
	h.end(c)
	got := h.b.SummaryOutcomes("g1")
	if len(got) != 1 {
		t.Fatalf("got %d outcomes, want 1: %+v", len(got), got)
	}
	o := got[0]
	if o.Outcome != OutcomeHit || o.Source != decide.SourceRules || o.Rule != "R4" || o.Target != "P1" ||
		o.Template != decide.TemplateSurvives || o.HorizonS != 15 || o.P != 0.7 || o.Acted || o.Backend != "rules" ||
		!o.RegisteredAt.Equal(t0) {
		t.Errorf("outcome = %+v", o)
	}
	if h.b.SummaryOutcomes("unknown") != nil {
		t.Errorf("an unknown game must give nil")
	}
}
