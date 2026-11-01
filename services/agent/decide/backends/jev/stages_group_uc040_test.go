package jev

import (
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// stages_group_uc040_test.go: jev's details stage for the group lever (#2467 PR 4;
// UC-040 A47; decide-contract §3 two-stage round). The target options are the group
// options, described by the players they cover; one option is answered locally.

func groupRequestJev(alive int) decide.Request {
	req := testRequest()
	if alive == 2 {
		for i := range req.State.Players {
			if req.State.Players[i].Serial == "MOCK0004" {
				req.State.Players[i].Alive = false
			}
		}
	}
	req.Questions = decide.FFAQuestions(req.State, append(append([]string(nil), fullInterventions...), decide.IntvGrantGroupShield))
	return req
}

// UC: UC-040 A47 BR-033
func TestPlanDetails_GroupLever(t *testing.T) {
	t.Run("UC-040/A47 three alive: jev picks among the group options", func(t *testing.T) {
		p := detailsRequest(groupRequestJev(3), decide.IntvGrantGroupShield)
		if len(p.ask.Questions) != 1 || p.ask.Questions[0].ID != decide.QuestionFFATarget {
			t.Fatalf("ask = %+v, want only the target", p.ask.Questions)
		}
		want := []string{"group:all", "group:all_except:MOCK0002", "group:all_except:MOCK0003", "group:all_except:MOCK0004"}
		if q := p.ask.Questions[0]; !reflect.DeepEqual(q.Options, want) || !strings.Contains(q.Text, "Which alive players should it cover?") {
			t.Fatalf("target question = %+v", q)
		}
		if len(p.local) != 1 || p.local[0].QuestionID != decide.QuestionFFAMagnitude {
			t.Fatalf("local = %+v, want the magnitude only (no bounds)", p.local)
		}
		// The details answer merges into a valid round.
		ans := decide.Answer{QuestionID: decide.QuestionFFATarget, Choice: "group:all_except:MOCK0003",
			Probabilities: map[string]float64{"group:all_except:MOCK0003": 0.6, "group:all": 0.4}, Confidence: 0.6}
		iv := decide.Answer{QuestionID: decide.QuestionFFAIntervention, Choice: decide.IntvGrantGroupShield,
			Probabilities: map[string]float64{decide.IntvGrantGroupShield: 0.8, decide.OptionNoop: 0.2}, Confidence: 0.8}
		resp, err := mergeAnswers(groupRequestJev(3), []decide.Answer{iv}, []decide.Answer{ans}, p.local)
		if err != nil {
			t.Fatalf("merge: %v", err)
		}
		if a, _ := resp.Find(decide.QuestionFFATarget); a.Choice != "group:all_except:MOCK0003" {
			t.Fatalf("target = %q", a.Choice)
		}
	})
	t.Run("UC-040/A47 two alive: group:all is answered locally", func(t *testing.T) {
		p := detailsRequest(groupRequestJev(2), decide.IntvGrantGroupShield)
		if len(p.ask.Questions) != 0 {
			t.Fatalf("ask = %+v, want nothing (one call)", p.ask.Questions)
		}
		var target decide.Answer
		for _, a := range p.local {
			if a.QuestionID == decide.QuestionFFATarget {
				target = a
			}
		}
		if target.Choice != decide.TargetGroupAll || target.Confidence != 1 {
			t.Fatalf("local target = %+v, want group:all at 1", target)
		}
	})
	t.Run("UC-040/A48 no group option: none at confidence 0", func(t *testing.T) {
		p := detailsRequest(testRequest(), decide.IntvGrantGroupShield)
		for _, a := range p.local {
			if a.QuestionID == decide.QuestionFFATarget && (a.Choice != decide.TargetNone || a.Confidence != 0) {
				t.Fatalf("local target = %+v, want none at 0", a)
			}
		}
	})
	t.Run("UC-040/A47 the criteria describe each option by the players it covers, no verdict", func(t *testing.T) {
		req := groupRequestJev(3)
		q := detailsRequest(req, decide.IntvGrantGroupShield).ask.Questions[0]
		crit := choiceCriteria(q, req.State, decide.IntvGrantGroupShield, "")
		if crit[0].description != "All 3 alive players: MOCK0002, MOCK0003, MOCK0004." {
			t.Fatalf("group:all = %q", crit[0].description)
		}
		if d := crit[2].description; !strings.HasPrefix(d, "Every alive player except MOCK0003: MOCK0002, MOCK0004. Left out: Player MOCK0003") {
			t.Fatalf("all_except = %q", d)
		}
		for _, c := range crit {
			for _, verdict := range []string{"wasted", "matters most", "unfair"} {
				if strings.Contains(c.description, verdict) {
					t.Fatalf("%s carries a verdict: %q", c.option, c.description)
				}
			}
		}
	})
}
