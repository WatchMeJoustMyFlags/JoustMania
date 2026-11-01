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
// one goal sentence in both gate criteria; without it every body is the one
// pinned by the existing fixtures. Goal-on stage 1 is pinned by
// testdata/goal_request_intervention.json.
//
//	go test ./decide/backends/jev -run TestGoalCriterion_Fixture -update-goal-fixtures

var updateGoalFixtures = flag.Bool("update-goal-fixtures", false, "rewrite testdata/goal_*.json")

func testGoalLines() *decide.GoalLines {
	return &decide.GoalLines{
		Profile: "long_running_game",
		Source:  "from_objectives",
		Goal:    "long_running_game duration>=180s activity=0.30..0.60 elimination_pace<=1/min",
		Now:     "duration=95s activity=0.42 elimination_pace=0.6/min closeness=0.21",
	}
}

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
	t.Run("UC-042/A12 goal-on stage 1 request is pinned", func(t *testing.T) {
		out, err := encodeRequest(fixtureModel, splitStages(goalRequest("")).first, "")
		if err != nil {
			t.Fatal(err)
		}
		const fixture = "goal_request_intervention.json"
		if *updateGoalFixtures {
			pretty, _ := json.MarshalIndent(decodeWire(t, out.Body), "", "  ")
			if err := os.WriteFile(filepath.Join("testdata", fixture), append(pretty, '\n'), 0o644); err != nil {
				t.Fatal(err)
			}
			return
		}
		jsonEqual(t, out.Body, fixture)
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
			want := jevGoal{Profile: goalLine.Profile, Targets: goalLine.Goal, Now: goalLine.Now}
			if wire.State.Goal == nil || *wire.State.Goal != want {
				t.Fatalf("state.goal = %+v, want %+v", wire.State.Goal, want)
			}
			gate, _ := wire.Questions[GateQuestionID].Criteria.(map[string]any)
			base := noulCriteria[GateQuestionID]
			for side, orig := range map[string]string{"true": base.True, "false": base.False} {
				got, _ := gate[side].(string)
				if !strings.HasPrefix(got, orig+" ") {
					t.Errorf("gate %s must keep its criterion and append the goal: %q", side, got)
				}
				if !strings.Contains(got, goalLine.Goal) || !strings.Contains(got, goalLine.Now) {
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
		if wire.State.Goal == nil || wire.State.Goal.Targets != goalLine.Goal {
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
