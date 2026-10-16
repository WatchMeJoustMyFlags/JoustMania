package jev

import (
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

func TestSplitStages(t *testing.T) {
	first, details, stage := splitStages(testRequest())
	if stage != StageIntervention || len(first.Questions) != 1 || first.Questions[0].ID != decide.QuestionFFAIntervention || len(details) != 2 {
		t.Errorf("FFA: stage %q first %v details %d", stage, first.Questions, len(details))
	}
	if first.Context != nil {
		t.Error("stage requests never carry Request.Context")
	}
	req, _, stage := splitStages(noulRequest())
	if stage != StageSingle || len(req.Questions) != 2 {
		t.Errorf("noul: stage %q", stage)
	}
}

func TestPlanDetails(t *testing.T) {
	req := testRequest()
	for _, c := range []struct {
		iv    string
		ask   []string
		local []string
	}{
		{"noop", nil, []string{decide.QuestionFFATarget, decide.QuestionFFAMagnitude}},
		{"grant_shield", []string{decide.QuestionFFATarget, decide.QuestionFFAMagnitude}, nil},
		{"send_controller_effect", []string{decide.QuestionFFATarget}, []string{decide.QuestionFFAMagnitude}},
		{"adjust_music_tempo", []string{decide.QuestionFFAMagnitude}, []string{decide.QuestionFFATarget}},
		{"end_game", nil, []string{decide.QuestionFFATarget, decide.QuestionFFAMagnitude}},
	} {
		p := detailsRequest(req, c.iv)
		var ask, local []string
		for _, q := range p.ask.Questions {
			ask = append(ask, q.ID)
			if q.ID == decide.QuestionFFATarget && !reflect.DeepEqual(q.Options, []string{"MOCK0002", "MOCK0003", "MOCK0004"}) {
				t.Errorf("%s: target options %v, want the alive players only", c.iv, q.Options)
			}
		}
		for _, a := range p.local {
			local = append(local, a.QuestionID)
		}
		if !reflect.DeepEqual(ask, c.ask) || !reflect.DeepEqual(local, c.local) {
			t.Errorf("%s: ask %v local %v", c.iv, ask, local)
		}
	}
}

func TestLocalTarget(t *testing.T) {
	q := decide.Question{ID: decide.QuestionFFATarget, Kind: decide.KindChoice, Options: []string{"none", "P1"}}
	if a := localTarget(q, "grant_shield", []string{"P1"}); a.Choice != "P1" || a.Confidence != 1 || a.Probabilities["none"] != 0 {
		t.Errorf("single candidate: %+v", a)
	}
	if a := localTarget(q, "grant_shield", nil); a.Choice != "none" || a.Confidence != 0 {
		t.Errorf("no candidate: %+v", a)
	}
	if a := localTarget(q, "end_game", []string{"P1"}); a.Choice != "none" || a.Confidence != 1 || len(a.Probabilities) != 2 {
		t.Errorf("session: %+v", a)
	}
}

func TestMagnitudeLevels(t *testing.T) {
	if got := magnitudeLevels("grant_shield"); !reflect.DeepEqual(got, []float64{3, 4.75, 6.5, 8.25, 10}) {
		t.Errorf("grant_shield = %v", got)
	}
	if got := magnitudeLevels("adjust_global_sensitivity"); !reflect.DeepEqual(got, []float64{0, 1, 2, 3, 4}) {
		t.Errorf("global sensitivity = %v", got)
	}
	if magnitudeLevels("end_game") != nil {
		t.Error("no bounds, no levels")
	}
	instr, ladder := conditionedMagnitude("adjust_player_sensitivity", magnitudeLevels("adjust_player_sensitivity"))
	if !strings.HasPrefix(instr, "The agent will apply adjust_player_sensitivity (") || len(ladder) != 5 {
		t.Fatalf("%q %v", instr, ladder)
	}
	if ladder[0] != "The lowest setting: 0.7 factor (player most protected)." ||
		ladder[1] != "A low setting: 0.9 factor (toward player most protected)." ||
		ladder[3] != "A high setting: 1.3 factor (toward player most handicapped)." {
		t.Errorf("ladder = %q", ladder)
	}
}

func TestDescribeCandidates(t *testing.T) {
	st := testRequest().State
	shield := describeCandidates(st, "grant_shield")
	if _, ok := shield["MOCK0001"]; ok {
		t.Error("an eliminated player is no candidate")
	}
	if !strings.Contains(shield["MOCK0003"], "Near the death threshold") || !strings.Contains(shield["MOCK0003"], "protecting this player now matters most") ||
		!strings.Contains(shield["MOCK0002"], "protection would be wasted") {
		t.Errorf("shield = %v", shield)
	}
	penalty := describeCandidates(st, "soft_penalty")
	if !strings.Contains(penalty["MOCK0002"], "Leading with the most headroom: pressure here") || !strings.Contains(penalty["MOCK0003"], "pile on") {
		t.Errorf("penalty = %v", penalty)
	}
	balance := describeCandidates(st, "set_player_handicap")
	if !strings.Contains(balance["MOCK0003"], "helping this player") || !strings.Contains(balance["MOCK0002"], "reining this player in") ||
		!strings.Contains(balance["MOCK0004"], "little to balance") {
		t.Errorf("balance = %v", balance)
	}
	if fx := describeCandidates(st, "send_controller_effect")["MOCK0003"]; strings.Contains(fx, "matters most") {
		t.Errorf("no suitability for an unfamilied intervention: %q", fx)
	}
}
