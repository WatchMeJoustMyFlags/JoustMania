package jev

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// UC: UC-042 A12 BR-009
// criteria_goal_fr201_test.go: jev's goal criterion (#2498, FR-201,
// decide-contract.md §14.4). A request with Request.Goal (set only under
// decision.goal_in_prompt) carries the goal as structured state (state.goal) and
// one goal sentence in both gate criteria, with the out-of-band comparison done
// in code (GoalLines.OffBand); without it every body is the one pinned by the
// existing fixtures. Goal-on stage 1 is pinned by
// testdata/goal_request_intervention.json (early game: duration 95 s against a
// 180 s minimum cannot be judged yet, nothing is off, no push to act) and
// testdata/goal_offband_request_intervention.json (activity 0.85 against
// 0.30..0.60 is named).
//
//	go test ./decide/backends/jev -run TestGoalCriterion_Fixture -update-goal-fixtures

var updateGoalFixtures = flag.Bool("update-goal-fixtures", false, "rewrite testdata/goal_*.json")

// testGoalLines is an early game on track: duration 95 s is below the 180 s
// minimum, which cannot be judged before the end, so nothing is off.
func testGoalLines() *decide.GoalLines {
	return &decide.GoalLines{
		Profile: "long_running_game",
		Source:  "from_objectives",
		Goal:    "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min",
		Now:     "duration=95s activity=0.42 elimination_pace=0.6/min closeness=0.21",
	}
}

// offBandGoalLines is the same game with activity clearly above its band.
func offBandGoalLines() *decide.GoalLines {
	g := testGoalLines()
	g.Now = "duration=95s activity=0.85 elimination_pace=0.6/min closeness=0.21"
	g.OffBand = []string{"activity"}
	return g
}

const (
	testTargets = "duration>=180s activity=0.30..0.60 elimination_pace<=1/min"
	onTrack     = "The goal is on track: no term that can be judged yet is outside its target band."
)

func goalRequest(profile string) decide.Request {
	req := testRequest()
	req.PromptProfile = profile
	req.Goal = testGoalLines()
	return req
}

func decodeWire(t *testing.T, body []byte) map[string]any {
	t.Helper()
	var v map[string]any
	if err := json.Unmarshal(body, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

func TestGoalCriterion_Fixture(t *testing.T) {
	for _, tc := range []struct {
		name, fixture string
		goal          *decide.GoalLines
	}{
		{"early game on track", "goal_request_intervention.json", testGoalLines()},
		{"activity off its band", "goal_offband_request_intervention.json", offBandGoalLines()},
	} {
		t.Run("UC-042/A12 goal-on stage 1 request is pinned: "+tc.name, func(t *testing.T) {
			req := goalRequest("")
			req.Goal = tc.goal
			out, err := encodeRequest(fixtureModel, splitStages(req).first, "")
			if err != nil {
				t.Fatal(err)
			}
			if *updateGoalFixtures {
				pretty, _ := json.MarshalIndent(decodeWire(t, out.Body), "", "  ")
				if err := os.WriteFile(filepath.Join("testdata", tc.fixture), append(pretty, '\n'), 0o644); err != nil {
					t.Fatal(err)
				}
				return
			}
			jsonEqual(t, out.Body, tc.fixture)
		})
	}
}

// UC: UC-042 A12
// TestGoalCriterion_NoActPushOnTrack: an early game whose only gap is a duration
// minimum it cannot have reached yet gets no push toward acting; an out-of-band
// term is named on the yes side only (#2498 review).
func TestGoalCriterion_NoActPushOnTrack(t *testing.T) {
	base := noulCriteria[GateQuestionID]
	line := func(g *decide.GoalLines) string {
		return "Goal long_running_game: targets " + testTargets + "; measured now " + g.Now + "."
	}
	t.Run("UC-042/A12 on track: the yes side gains only the state sentence", func(t *testing.T) {
		g := testGoalLines()
		got := withGoal(GateQuestionID, base, g)
		if got.True != base.True+" "+line(g) {
			t.Errorf("yes side = %q", got.True)
		}
		if got.False != base.False+" "+line(g)+" "+onTrack {
			t.Errorf("no side = %q", got.False)
		}
	})
	t.Run("UC-042/A12 an out-of-band term is named on the yes side", func(t *testing.T) {
		g := offBandGoalLines()
		got := withGoal(GateQuestionID, base, g)
		if got.True != base.True+" "+line(g)+" Outside its target band now: activity." {
			t.Errorf("yes side = %q", got.True)
		}
		if got.False != base.False+" "+line(g) {
			t.Errorf("no side = %q", got.False)
		}
	})
}

func TestGoalCriterion(t *testing.T) {
	goalLine := testGoalLines()
	for _, profile := range []string{"", decide.PromptProfileNeutral} {
		name := profile
		if name == "" {
			name = "v1"
		}
		t.Run("UC-042/A12 "+name+": state.goal and the gate's goal sentence, nothing else changes", func(t *testing.T) {
			on, err := encodeRequest(fixtureModel, splitStages(goalRequest(profile)).first, "")
			if err != nil {
				t.Fatal(err)
			}
			offReq := goalRequest(profile)
			offReq.Goal = nil
			off, err := encodeRequest(fixtureModel, splitStages(offReq).first, "")
			if err != nil {
				t.Fatal(err)
			}
			if strings.Contains(string(off.Body), `"goal"`) || strings.Contains(string(off.Body), "Goal long_running_game") {
				t.Fatalf("a goal-off body carries the goal: %s", off.Body)
			}
			var wire jevRequest
			if err := json.Unmarshal(on.Body, &wire); err != nil {
				t.Fatal(err)
			}
			want := jevGoal{Profile: goalLine.Profile, Targets: testTargets, Now: goalLine.Now}
			if wire.State.Goal == nil || !reflect.DeepEqual(*wire.State.Goal, want) {
				t.Fatalf("state.goal = %+v, want %+v", wire.State.Goal, want)
			}
			gate, _ := wire.Questions[GateQuestionID].Criteria.(map[string]any)
			base := noulCriteria[GateQuestionID]
			for side, orig := range map[string]string{"true": base.True, "false": base.False} {
				got, _ := gate[side].(string)
				if !strings.HasPrefix(got, orig+" ") {
					t.Errorf("gate %s must keep its criterion and append the goal: %q", side, got)
				}
				if !strings.Contains(got, testTargets) || !strings.Contains(got, goalLine.Now) || strings.Contains(got, "targets long_running_game") {
					t.Errorf("gate %s must name the targets and the measured values: %q", side, got)
				}
				// The goal sentence describes state; it never names a lever.
				for _, iv := range decide.Catalog {
					if strings.Contains(strings.TrimPrefix(got, orig), iv.Name) {
						t.Errorf("gate %s goal sentence names the lever %q", side, iv.Name)
					}
				}
			}
			// Only state.goal and the gate criteria differ.
			onV, offV := decodeWire(t, on.Body), decodeWire(t, off.Body)
			delete(onV["state"].(map[string]any), "goal")
			for _, v := range []map[string]any{onV, offV} {
				delete(v["questions"].(map[string]any)[GateQuestionID].(map[string]any), "criteria")
			}
			if !reflect.DeepEqual(onV, offV) {
				t.Errorf("the goal changed more than state.goal and the gate criteria:\non  %v\noff %v", onV, offV)
			}
		})
	}

	t.Run("UC-042/A12 the details stage keeps the goal in its state and asks no gate", func(t *testing.T) {
		ask := detailsRequest(goalRequest(""), "grant_shield").ask
		if ask.Goal == nil {
			t.Fatal("the details request lost the goal")
		}
		out, err := encodeRequest(fixtureModel, ask, "grant_shield")
		if err != nil {
			t.Fatal(err)
		}
		var wire jevRequest
		if err := json.Unmarshal(out.Body, &wire); err != nil {
			t.Fatal(err)
		}
		if wire.State.Goal == nil || wire.State.Goal.Targets != testTargets {
			t.Fatalf("details state.goal = %+v", wire.State.Goal)
		}
		offAsk := detailsRequest(testRequest(), "grant_shield").ask
		offOut, _ := encodeRequest(fixtureModel, offAsk, "grant_shield")
		onV := decodeWire(t, out.Body)
		delete(onV["state"].(map[string]any), "goal")
		if !reflect.DeepEqual(onV, decodeWire(t, offOut.Body)) {
			t.Error("the details stage must differ from goal-off only by state.goal")
		}
	})

	t.Run("UC-042/A12 a caller's noul gets no goal sentence", func(t *testing.T) {
		q := decide.Question{ID: "caller.noul", Kind: decide.KindNoul, Text: "Is it close?"}
		jq, err := encodeQuestion(q, goalRequest(""), "")
		if err != nil {
			t.Fatal(err)
		}
		if jq.Criteria != nil {
			t.Fatalf("a caller's noul has no criteria, got %v", jq.Criteria)
		}
	})
}
