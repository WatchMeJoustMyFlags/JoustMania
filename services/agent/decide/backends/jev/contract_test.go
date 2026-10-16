package jev

// Contract test: replays the fixtures in testdata/ through wire.go (and, for
// the responses, through the whole Decide path against an httptest server).
//
// The fixtures are HAND-WRITTEN placeholders in an ASSUMED OpenAI-compatible
// shape (each carries a "_fixture" marker saying so). When Jev's API docs and a
// key arrive: record real request/response bodies for testRequest(), drop them
// into testdata/ under the same names, rewrite wire.go, and update the
// expectations in this file. jev.go and the rest of the agent stay untouched.

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/decide/llmio"
)

const fixtureModel = "jev-test-model"

func loadFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("fixture %s: %v", name, err)
	}
	return b
}

// TestContract_Request: encodeRequest(testRequest()) matches
// testdata/request_ffa.json. The prompt texts and the schema are replaced by
// placeholders before comparing, so prompt wording changes in llmio do not
// break the contract (they are covered by llmio's own tests).
func TestContract_Request(t *testing.T) {
	out, err := encodeRequest(fixtureModel, testRequest())
	if err != nil {
		t.Fatal(err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.Body, &got); err != nil {
		t.Fatal(err)
	}
	p := llmio.Render(testRequest())
	if out.Content != p {
		t.Error("content capture must record the rendered prompt")
	}
	msgs := got["messages"].([]any)
	if msgs[0].(map[string]any)["content"] != p.System || msgs[1].(map[string]any)["content"] != p.User {
		t.Fatal("messages do not carry the rendered prompt")
	}
	msgs[0].(map[string]any)["content"] = "{{SYSTEM_PROMPT}}"
	msgs[1].(map[string]any)["content"] = "{{USER_PROMPT}}"
	js := got["response_format"].(map[string]any)["json_schema"].(map[string]any)
	wantSchema, _ := json.Marshal(llmio.AnswerSchema())
	gotSchema, _ := json.Marshal(js["schema"])
	if string(gotSchema) != string(wantSchema) {
		t.Errorf("schema = %s", gotSchema)
	}
	js["schema"] = "{{ANSWER_SCHEMA}}"

	var want map[string]any
	if err := json.Unmarshal(loadFixture(t, "request_ffa.json"), &want); err != nil {
		t.Fatal(err)
	}
	delete(want, "_fixture")
	if !reflect.DeepEqual(got, want) {
		g, _ := json.MarshalIndent(got, "", "  ")
		t.Errorf("request does not match testdata/request_ffa.json:\n%s", g)
	}
}

// responseContract is the expected outcome of replaying one response fixture.
type responseContract struct {
	fixture   string
	errType   string // "" = success
	isInvalid bool
	check     func(t *testing.T, res wireResult)
}

var responseContracts = []responseContract{
	{fixture: "response_ok.json", check: func(t *testing.T, res wireResult) {
		r := res.Response
		if r.Backend != Name {
			t.Errorf("backend = %q", r.Backend)
		}
		if a, _ := r.Find(decide.QuestionFFAIntervention); a.Choice != "grant_shield" || a.Confidence != 0.88 ||
			a.Probabilities["grant_shield"] != 0.75 || a.Probabilities["noop"] != 0.25 {
			t.Errorf("intervention = %+v", a)
		}
		if a, _ := r.Find(decide.QuestionFFATarget); a.Choice != "MOCK0002" || a.Confidence != 0.8 {
			t.Errorf("target = %+v", a)
		}
		if a, _ := r.Find(decide.QuestionFFAMagnitude); a.Score != 0.5 || a.Confidence != 0.65 {
			t.Errorf("magnitude = %+v", a)
		}
		if r.Rationale == "" {
			t.Error("rationale lost")
		}
		if res.ID != "jev-resp-001" || res.Model != "jev-test-model-2026-10" {
			t.Errorf("id/model = %q/%q", res.ID, res.Model)
		}
		if res.Usage == nil || res.Usage.InputTokens != 850 || res.Usage.OutputTokens != 160 || res.Usage.ReasoningTokens != 30 {
			t.Errorf("usage = %+v", res.Usage)
		}
		if len(res.Reasoning) != 1 || res.OutputFinishReason != llmio.FinishStop {
			t.Errorf("reasoning/finish = %v/%q", res.Reasoning, res.OutputFinishReason)
		}
	}},
	{fixture: "response_invalid_answer.json", errType: "invalid_output", isInvalid: true},
	{fixture: "response_truncated.json", errType: "max_tokens", isInvalid: true},
	{fixture: "response_error.json", errType: "_OTHER"},
}

// TestContract_Responses replays every response fixture through
// decodeResponse.
func TestContract_Responses(t *testing.T) {
	for _, c := range responseContracts {
		t.Run(c.fixture, func(t *testing.T) {
			res, errType, err := decodeResponse(testRequest(), loadFixture(t, c.fixture))
			if errType != c.errType {
				t.Fatalf("errType = %q, want %q (err %v)", errType, c.errType, err)
			}
			if (err == nil) != (c.errType == "") {
				t.Fatalf("err = %v", err)
			}
			if err != nil && errors.Is(err, decide.ErrInvalid) != c.isInvalid {
				t.Errorf("errors.Is(ErrInvalid) = %v, want %v", !c.isInvalid, c.isInvalid)
			}
			if err == nil {
				if verr := decide.Validate(testRequest(), res.Response); verr != nil {
					t.Errorf("Validate: %v", verr)
				}
			}
			if c.check != nil {
				c.check(t, res)
			}
		})
	}
}

// TestContract_DecideReplay serves every response fixture from an httptest
// server and runs the full Decide path, so the fixtures exercise jev.go too.
func TestContract_DecideReplay(t *testing.T) {
	for _, c := range responseContracts {
		t.Run(c.fixture, func(t *testing.T) {
			api := newFakeAPI(t, 200, loadFixture(t, c.fixture), 0)
			resp, err := api.backend(fixtureModel, true).Decide(context.Background(), testRequest())
			if (err == nil) != (c.errType == "") {
				t.Fatalf("err = %v, want errType %q", err, c.errType)
			}
			if err == nil && resp.Backend != Name {
				t.Errorf("backend = %q", resp.Backend)
			}
			got := attrs(api.lastSpan(t))["error.type"].AsString()
			if got != c.errType {
				t.Errorf("error.type = %q, want %q", got, c.errType)
			}
		})
	}
}
