package prediction

import (
	"math"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/gamecontext"
)

// UC: UC-043 A21 BR-010
// arm_tally_uc043_test.go: at game end the Book hands the game's tally of ACTED
// decision predictions that scored hit or miss to the arm-calibration sink
// (#2479, #2498); counterfactuals, controls and unscorables never count.

type tallyCall struct {
	gameID      string
	scored, hit int
	brier       float64
}

func (h *harness) tallySink() *[]tallyCall {
	var calls []tallyCall
	h.b.SetArmTallySink(func(gameID string, scored, hits int, brier float64) {
		calls = append(calls, tallyCall{gameID, scored, hits, brier})
	})
	return &calls
}

// mixedGame registers, on a three-player game: an acted no_elimination that
// misses early (P3 out at 3 s), an acted survives(P1) and the same survives(P2)
// as a counterfactual (both hit at the horizon), and a control survives(P1).
func mixedGame(h *harness) gamecontext.GameContext {
	c := game("P1", "P2", "P3")
	h.register(pred(decide.TemplateNoElimination, 15, ""), c, true)
	h.register(pred(decide.TemplateSurvives, 15, "P1"), c, true)
	h.register(pred(decide.TemplateSurvives, 15, "P2"), c, false)
	ctl := decide.NewPrediction(decide.TemplateSurvives, 15, 0.5, decide.SourceControl, "", "P1")
	if _, _, ok := h.b.Register(Registration{Backend: BackendNone, Prediction: ctl, Context: c}); !ok {
		h.t.Fatal("control not registered")
	}
	h.at(4 * time.Second)
	live := eliminate(c, "P3", 3*time.Second)
	h.b.Observe(live)
	h.at(20 * time.Second)
	h.setLive(&live)
	h.fire()
	return live
}

func TestBook_ArmTally(t *testing.T) {
	t.Run("UC-043/A21 acted hit and miss only, with their Brier sum", func(t *testing.T) {
		h := newHarness(t)
		calls := h.tallySink()
		live := mixedGame(h)
		results := h.end(live)
		// Not vacuous: the counterfactual and the control scored too.
		var cfScored, ctlScored bool
		for _, r := range results {
			scored := r.Outcome == OutcomeHit || r.Outcome == OutcomeMiss
			if !r.Acted && r.Prediction.Source != decide.SourceControl && scored {
				cfScored = true
			}
			if r.Prediction.Source == decide.SourceControl && scored {
				ctlScored = true
			}
		}
		if !cfScored || !ctlScored {
			t.Fatalf("counterfactual scored %v, control scored %v: the filter is untested (%+v)", cfScored, ctlScored, results)
		}
		var wantScored, wantHits int
		var wantBrier float64
		for _, r := range results {
			if !r.Acted || r.Prediction.Source == decide.SourceControl {
				continue
			}
			switch r.Outcome {
			case OutcomeHit:
				wantScored, wantHits = wantScored+1, wantHits+1
				wantBrier += (r.Prediction.P - 1) * (r.Prediction.P - 1)
			case OutcomeMiss:
				wantScored++
				wantBrier += r.Prediction.P * r.Prediction.P
			}
		}
		if wantScored != 2 || wantHits != 1 {
			t.Fatalf("fixture drift: acted scored %d, hits %d, want 2 and 1 (%+v)", wantScored, wantHits, results)
		}
		if len(*calls) != 1 {
			t.Fatalf("sink called %d times, want once", len(*calls))
		}
		got := (*calls)[0]
		if got.gameID != live.SessionID || got.scored != wantScored || got.hit != wantHits || math.Abs(got.brier-wantBrier) > 1e-9 {
			t.Fatalf("tally = %+v, want %s %d/%d brier %v", got, live.SessionID, wantScored, wantHits, wantBrier)
		}
	})
	t.Run("UC-043/A21 a stale end hands nothing on", func(t *testing.T) {
		h := newHarness(t)
		calls := h.tallySink()
		live := mixedGame(h)
		live.EndReason = gamecontext.EndReasonStale
		h.b.OnGameEnd(live)
		if len(*calls) != 0 {
			t.Fatalf("stale end handed on %+v", *calls)
		}
	})
	t.Run("UC-043/A21 a game without an acted scored prediction hands nothing on", func(t *testing.T) {
		h := newHarness(t)
		calls := h.tallySink()
		c := game("P1", "P2")
		h.register(pred(decide.TemplateSurvives, 15, "P1"), c, false)
		h.end(c)
		if len(*calls) != 0 {
			t.Fatalf("handed on %+v", *calls)
		}
	})
	t.Run("UC-043/A21 no sink wired: game end works as before", func(t *testing.T) {
		h := newHarness(t)
		if rs := h.end(mixedGame(h)); len(rs) == 0 {
			t.Fatal("no results")
		}
	})
}
