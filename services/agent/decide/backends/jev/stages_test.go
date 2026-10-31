package jev

import (
	"math"
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

func TestSplitStages(t *testing.T) {
	p := splitStages(testRequest())
	var ids []string
	for _, q := range p.first.Questions {
		ids = append(ids, q.ID)
	}
	if p.stage != StageIntervention || !reflect.DeepEqual(ids, []string{GateQuestionID, decide.QuestionFFAIntervention}) || len(p.details) != 2 {
		t.Errorf("FFA: stage %q first %v details %d", p.stage, ids, len(p.details))
	}
	if p.gated == nil || p.gated.Options[0] != decide.OptionNoop {
		t.Fatalf("gated = %+v, want the original ffa.intervention", p.gated)
	}
	iv := p.first.Questions[1]
	if contains(iv.Options, decide.OptionNoop) || len(iv.Options) != len(p.gated.Options)-1 || iv.Text != gatedInterventionInstructions {
		t.Errorf("stage-1 intervention = %+v, want every option but noop", iv)
	}
	if p.first.Context != nil {
		t.Error("stage requests never carry Request.Context")
	}
	p = splitStages(noulRequest())
	if p.stage != StageSingle || len(p.first.Questions) != 2 || p.gated != nil {
		t.Errorf("noul: stage %q", p.stage)
	}
	// Not gateable: noop is the only option, or noop is not offered.
	for _, opts := range [][]string{{decide.OptionNoop}, {"grant_shield", "end_game"}} {
		req := testRequest()
		req.Questions[0].Options = opts
		if p := splitStages(req); p.gated != nil || len(p.first.Questions) != 1 || !reflect.DeepEqual(p.first.Questions[0].Options, opts) {
			t.Errorf("options %v: gated %v, first %+v", opts, p.gated, p.first.Questions)
		}
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
		{"adjust_player_sensitivity", []string{decide.QuestionFFATarget, "ffa.magnitude.help", "ffa.magnitude.rein_in"}, nil},
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
	// I3: the neutral "no change" value is never an acting level.
	if got := magnitudeLevels("set_player_handicap"); !reflect.DeepEqual(got, []float64{0.75, 1.25, 1.5, 1.75}) {
		t.Errorf("handicap = %v (neutral 1.0 must be excluded)", got)
	}
	mag := decide.Question{ID: decide.QuestionFFAMagnitude, Kind: decide.KindScore, Range: [2]float64{0, 1}}
	instr, ladder := conditionedMagnitude(mag, "adjust_global_difficulty", "", magnitudeLevels("adjust_global_difficulty"))
	if !strings.HasPrefix(instr, "The agent will apply adjust_global_difficulty (") || len(ladder) != 5 {
		t.Fatalf("%q %v", instr, ladder)
	}
	if ladder[0] != "The lowest setting: 0.5 factor (easiest game)." || ladder[1] != "A low setting: 0.875 factor (toward easiest game)." {
		t.Errorf("ladder = %q", ladder)
	}
}

// TestStrengthLevels: a two-way lever's half-ladders stay on their side of the
// neutral, weakest first.
func TestStrengthLevels(t *testing.T) {
	for _, c := range []struct {
		iv, dir string
		want    []float64
	}{
		{"adjust_player_sensitivity", directionHelp, []float64{0.9, 0.8, 0.7}},
		{"adjust_player_sensitivity", directionReinIn, []float64{1.17, 1.33, 1.5}},
		{"set_player_handicap", directionHelp, []float64{1.25, 1.5, 1.75}},
		{"set_player_handicap", directionReinIn, []float64{0.92, 0.83, 0.75}},
	} {
		if got := strengthLevels(c.iv, c.dir); !reflect.DeepEqual(got, c.want) {
			t.Errorf("%s %s = %v, want %v", c.iv, c.dir, got, c.want)
		}
	}
	if strengthLevels("grant_shield", directionHelp) != nil {
		t.Error("one-way lever has no half-ladder")
	}
	q := decide.Question{ID: strengthQuestionID(directionHelp), Kind: decide.KindScore, Range: [2]float64{0, 1}}
	instr, ladder := scoreLadder(q, decide.Request{}, "adjust_player_sensitivity")
	if !strings.Contains(instr, "to help a struggling player") || ladder[0] != "A slight change: 0.9 factor (neutral is 1)." {
		t.Errorf("%q %q", instr, ladder)
	}
}

// TestDirection: help for the struggling / near-threshold player, rein in the
// calmest, help when ambiguous or unknown.
// UC: UC-040 A13
func TestDirection(t *testing.T) {
	st := testRequest().State // MOCK0003 struggling, MOCK0002 most headroom, MOCK0004 in between
	for target, want := range map[string]string{
		"MOCK0003": directionHelp, "MOCK0002": directionReinIn, "MOCK0004": directionHelp, "none": directionHelp, "": directionHelp,
	} {
		if got := direction(st, target); got != want {
			t.Errorf("direction(%s) = %s, want %s", target, got, want)
		}
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
	if !strings.Contains(penalty["MOCK0002"], "Most headroom below the death threshold right now: pressure here") || !strings.Contains(penalty["MOCK0003"], "pile on") {
		t.Errorf("penalty = %v", penalty)
	}
	balance := describeCandidates(st, "set_player_handicap")
	if !strings.Contains(balance["MOCK0003"], "helping this player") || !strings.Contains(balance["MOCK0002"], "reining this player in") ||
		!strings.Contains(balance["MOCK0004"], "little to balance") || strings.Contains(balance["MOCK0002"], "lead") {
		t.Errorf("balance = %v", balance)
	}
	if fx := describeCandidates(st, "send_controller_effect")["MOCK0003"]; strings.Contains(fx, "matters most") {
		t.Errorf("no suitability for an unfamilied intervention: %q", fx)
	}
	// I2: never suggest eliminating a player because they are calm.
	for serial, d := range describeCandidates(st, "eliminate_player") {
		if strings.Contains(d, "pressure") || strings.Contains(d, "keeps the game close") {
			t.Errorf("eliminate_player suitability for %s: %q", serial, d)
		}
	}
	// S1: a recently shielded player is not suggested for another shield.
	st.Players[2].Shielded = true // MOCK0003
	if d := describeCandidates(st, "grant_shield")["MOCK0003"]; !strings.Contains(d, "another shield would be wasted") || strings.Contains(d, "matters most") {
		t.Errorf("shielded MOCK0003 = %q", d)
	}
}

// TestGateAnswer: the synthesised routing answer (#1281) for every p_yes and
// every possible stage-1 pick passes decide.Validate against the ORIGINAL
// question (argmax rule, full option coverage), chooses the intervention iff
// p_yes > 0.5, and carries the acting confidence p_yes (1 - p_yes for noop).
// Spec: decide-contract.md §3.1, UC-040 A13, FR-046, FR-047.
// UC: UC-040 A13
func TestGateAnswer(t *testing.T) {
	req := testRequest()
	orig := req.Questions[0]
	for _, best := range orig.Options[1:] {
		for _, pYes := range []float64{0, 0.01, 0.3, 0.49, 0.5, 0.500001, 0.51, 0.7, 0.8, 0.99, 1} {
			stage1 := decide.Answer{QuestionID: orig.ID, Choice: best, Probabilities: map[string]float64{best: 0.4}}
			a := gateAnswer(orig, pYes, stage1)
			resp := decide.Response{Answers: []decide.Answer{a,
				{QuestionID: decide.QuestionFFATarget, Choice: decide.TargetNone, Probabilities: map[string]float64{decide.TargetNone: 1}, Confidence: 1},
				{QuestionID: decide.QuestionFFAMagnitude, Confidence: 1}}}
			if err := decide.Validate(req, resp); err != nil {
				t.Fatalf("%s p_yes %v: %v", best, pYes, err)
			}
			if len(a.Probabilities) != len(orig.Options) {
				t.Errorf("%s p_yes %v: %d options covered, want %d", best, pYes, len(a.Probabilities), len(orig.Options))
			}
			var mass float64
			for o, p := range a.Probabilities {
				if o != decide.OptionNoop && o != best && p != 0 {
					t.Errorf("%s p_yes %v: p(%s) = %v, want 0 (no factorised split)", best, pYes, o, p)
				}
				mass += p
			}
			acts := pYes > 0.5
			if (a.Choice == best) != acts || (a.Choice == decide.OptionNoop) == acts {
				t.Errorf("p_yes %v: choice %s", pYes, a.Choice)
			}
			wantConf := pYes
			if !acts {
				wantConf = 1 - pYes
			}
			if math.Abs(a.Confidence-wantConf) > 1e-12 || math.Abs(mass-1) > 1e-12 {
				t.Errorf("p_yes %v: confidence %v (want %v), mass %v", pYes, a.Confidence, wantConf, mass)
			}
		}
	}
}

func TestResolveGate(t *testing.T) {
	p := splitStages(testRequest())
	yes := 0.8
	answers := []decide.Answer{
		{QuestionID: GateQuestionID, Noul: &yes, Confidence: 0.6},
		{QuestionID: decide.QuestionFFAIntervention, Choice: "grant_shield", Probabilities: map[string]float64{"grant_shield": 1}, Confidence: 1},
	}
	out, stage1, err := p.resolveGate(answers)
	if err != nil || len(out) != 1 || out[0].Choice != "grant_shield" || stage1.Choice != "grant_shield" {
		t.Errorf("resolveGate = %+v, %+v, %v", out, stage1, err)
	}
	if _, _, err := p.resolveGate(answers[1:]); err == nil {
		t.Error("a missing gate answer must fail the round")
	}
	if _, _, err := p.resolveGate(answers[:1]); err == nil {
		t.Error("a missing intervention answer must fail the round")
	}
}
