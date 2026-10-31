package llmio

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

// prediction_uc040_test.go: the model-stated prediction on the llmio side
// (#2479, UC-040 A45, BR-034; decide-contract.md §3, §4.1).

func askRequest() decide.Request {
	req := testRequest()
	req.PredictionAsk = decide.PredictionQuestion(req.State)
	return req
}

// answerWith is validAnswer (grant_shield on MOCK0002) plus extra answer entries.
func answerWith(extra ...string) []byte {
	head := `{"answers": [
    {"id": "ffa.intervention", "choice": "grant_shield", "score": 0,
     "probabilities": [{"option": "grant_shield", "p": 0.7}, {"option": "noop", "p": 0.3}], "confidence": 0.85},
    {"id": "ffa.target", "choice": "MOCK0002", "score": 0,
     "probabilities": [{"option": "MOCK0002", "p": 0.9}, {"option": "MOCK0001", "p": 0.1}], "confidence": 0.8},
    {"id": "ffa.magnitude", "choice": "", "score": 0.5, "probabilities": [], "confidence": 0.6}`
	for _, e := range extra {
		head += ",\n    " + e
	}
	return []byte(head + `], "rationale": "shield the duel"}`)
}

func predictionEntry(choice, confidence string) string {
	return `{"id": "ffa.prediction", "choice": "` + choice + `", "score": 0, "probabilities": [], "confidence": ` + confidence + `}`
}

// UC: UC-040 A45 BR-034
// TestRender_PredictionAsk: the ask is one more question item, after the FFA
// questions; the system prompt and the answer schema do not change.
func TestRender_PredictionAsk(t *testing.T) {
	off, on := Render(testRequest()), Render(askRequest())
	if on.System != off.System {
		t.Fatal("UC-040/A45 the ask must not change the system prompt")
	}
	head := strings.TrimSuffix(off.User, "</questions>")
	if !strings.HasPrefix(on.User, head) || !strings.HasSuffix(on.User, "</questions>") {
		t.Fatalf("UC-040/A45 the ask must be appended after the v1 questions:\n%s", on.User)
	}
	item := strings.TrimSuffix(strings.TrimPrefix(on.User, head), "</questions>")
	if !strings.HasPrefix(item, "- id: ffa.prediction\n  kind: choice\n  options: [\"none\", ") ||
		!strings.Contains(item, `"survives_15s"`) || !strings.Contains(item, "\n    survives: ") {
		t.Fatalf("UC-040/A45 ask item = %q", item)
	}
}

// UC: UC-040 A45 BR-034
// TestParse_PredictionNeverBlocks: with the ask sent, a missing, bad or
// inconsistent prediction still yields the same valid decision, with
// PredictionDropped set and no prediction.
func TestParse_PredictionNeverBlocks(t *testing.T) {
	base, err := Parse(testRequest(), answerWith(), "claude")
	if err != nil {
		t.Fatalf("baseline Parse: %v", err)
	}
	for _, tc := range []struct {
		name  string
		extra []string
		want  string
	}{
		{"UC-040/A45 BR-034 missing prediction", nil, decide.PredictionDropMissing},
		{"UC-040/A45 BR-034 option not offered", []string{predictionEntry("survives_99s", "0.7")}, decide.PredictionDropBadOption},
		{"UC-040/A45 BR-034 free text option", []string{predictionEntry("the player survives", "0.7")}, decide.PredictionDropBadOption},
		{"UC-040/A45 BR-034 confidence out of range", []string{predictionEntry("survives_15s", "1.5")}, decide.PredictionDropBadConfidence},
		{"UC-040/A45 BR-034 confidence missing", []string{`{"id": "ffa.prediction", "choice": "survives_15s", "score": 0, "probabilities": []}`}, decide.PredictionDropBadConfidence},
		{"UC-040/A45 BR-034 answered twice", []string{predictionEntry("survives_15s", "0.7"), predictionEntry("none", "0.5")}, decide.PredictionDropInconsistent},
		{"UC-040/A45 BR-034 wrongly typed fields drop only the prediction", []string{`{"id": "ffa.prediction", "choice": "survives_15s", "score": "x", "probabilities": [{"option": 1}], "confidence": 0.5}`}, ""},
		{"UC-040/A45 BR-034 a non-string choice is a bad option", []string{`{"id": "ffa.prediction", "choice": 3, "score": 0, "probabilities": [], "confidence": 0.5}`}, decide.PredictionDropBadOption},
		{"UC-040/A45 BR-034 a string confidence is a bad confidence", []string{`{"id": "ffa.prediction", "choice": "survives_15s", "score": 0, "probabilities": [], "confidence": "high"}`}, decide.PredictionDropBadConfidence},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resp, err := Parse(askRequest(), answerWith(tc.extra...), "claude")
			if err != nil {
				t.Fatalf("Parse must not fail on a bad prediction: %v", err)
			}
			if tc.want == "" {
				// Only the choice and the confidence are read: the stray fields
				// neither drop the prediction nor block the decision.
				if resp.Prediction == nil || resp.PredictionDropped != "" {
					t.Fatalf("prediction = %+v dropped = %q, want the prediction kept", resp.Prediction, resp.PredictionDropped)
				}
			} else if resp.Prediction != nil || resp.PredictionDropped != tc.want {
				t.Fatalf("prediction = %+v dropped = %q, want dropped %q", resp.Prediction, resp.PredictionDropped, tc.want)
			}
			if !reflect.DeepEqual(resp.Answers, base.Answers) || resp.Rationale != base.Rationale {
				t.Fatalf("the decision changed: %+v vs %+v", resp.Answers, base.Answers)
			}
			if err := decide.Validate(askRequest(), resp); err != nil {
				t.Fatalf("the response must stay valid: %v", err)
			}
		})
	}
	t.Run("UC-040/A45 BR-034 wrong target: survives on a session-scoped choice", func(t *testing.T) {
		raw := []byte(`{"answers": [
    {"id": "ffa.intervention", "choice": "adjust_music_tempo", "score": 0, "probabilities": [{"option": "adjust_music_tempo", "p": 1}], "confidence": 0.9},
    {"id": "ffa.target", "choice": "none", "score": 0, "probabilities": [{"option": "none", "p": 1}], "confidence": 0.9},
    {"id": "ffa.magnitude", "choice": "", "score": 0.2, "probabilities": [], "confidence": 0.9},
    ` + predictionEntry("survives_15s", "0.8") + `], "rationale": "slow down"}`)
		resp, err := Parse(askRequest(), raw, "openai")
		if err != nil {
			t.Fatalf("Parse: %v", err)
		}
		if resp.Prediction != nil || resp.PredictionDropped != decide.PredictionDropNoTarget {
			t.Fatalf("prediction = %+v dropped = %q", resp.Prediction, resp.PredictionDropped)
		}
		if iv, _ := resp.Find(decide.QuestionFFAIntervention); iv.Choice != "adjust_music_tempo" {
			t.Fatalf("decision lost: %+v", resp.Answers)
		}
	})
	t.Run("UC-040/A45 BR-034 an invalid decision stays invalid with a good prediction", func(t *testing.T) {
		raw := []byte(strings.Replace(string(answerWith(predictionEntry("survives_15s", "0.7"))), `"choice": "MOCK0002"`, `"choice": "MOCK0009"`, 1))
		if _, err := Parse(askRequest(), raw, "claude"); err == nil {
			t.Fatal("a prediction must never make an invalid decision valid")
		}
	})
}

// UC: UC-040 A45 BR-034
// TestParse_ModelPrediction: a valid prediction becomes Response.Prediction with
// Source model; none states nothing; without the ask a stray answer is ignored.
func TestParse_ModelPrediction(t *testing.T) {
	t.Run("UC-040/A45 BR-034 valid prediction is the model's", func(t *testing.T) {
		resp, err := Parse(askRequest(), answerWith(predictionEntry("survives_15s", "0.72")), "claude")
		if err != nil {
			t.Fatalf("Parse: %v", err)
		}
		p := resp.Prediction
		if resp.PredictionDropped != "" || p == nil || p.Source != decide.SourceModel || p.Template != decide.TemplateSurvives ||
			p.HorizonS != 15 || p.P != 0.72 || p.Target != "MOCK0002" || p.Rule != "grant_shield" {
			t.Fatalf("prediction = %+v dropped = %q", p, resp.PredictionDropped)
		}
		if _, found := resp.Find(decide.QuestionFFAPrediction); found {
			t.Fatal("the prediction answer must not stay among the answers")
		}
	})
	t.Run("UC-040/A45 BR-034 none states nothing and drops nothing", func(t *testing.T) {
		resp, err := Parse(askRequest(), answerWith(predictionEntry("none", "0.5")), "claude")
		if err != nil || resp.Prediction != nil || resp.PredictionDropped != "" {
			t.Fatalf("resp = %+v err = %v", resp, err)
		}
	})
	t.Run("UC-040/A45 BR-034 without the ask a stray prediction is ignored", func(t *testing.T) {
		resp, err := Parse(testRequest(), answerWith(predictionEntry("survives_15s", "0.7")), "claude")
		if err != nil || resp.Prediction != nil || resp.PredictionDropped != "" {
			t.Fatalf("resp = %+v err = %v", resp, err)
		}
	})
	t.Run("UC-040/A45 BR-034 without the ask nothing is dropped", func(t *testing.T) {
		resp, err := Parse(testRequest(), answerWith(), "claude")
		if err != nil || resp.PredictionDropped != "" {
			t.Fatalf("resp = %+v err = %v", resp, err)
		}
	})
}

// UC: UC-040 A45 BR-034
// TestAnswerSchema_UnchangedByAsk: the schema has no prediction property (the
// ask is answered in the existing answers array).
func TestAnswerSchema_UnchangedByAsk(t *testing.T) {
	b, _ := json.Marshal(AnswerSchema())
	if strings.Contains(string(b), "prediction") {
		t.Fatalf("UC-040/A45 the schema must not name the prediction: %s", b)
	}
}
