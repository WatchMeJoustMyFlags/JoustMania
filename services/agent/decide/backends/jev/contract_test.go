package jev

// Contract test: replays the fixtures in testdata/ through wire.go (and, for
// the responses, through the whole Decide path against an httptest server).
//
// The fixtures are REAL Jev traffic, recorded with
//
//	JEV_API_KEY=... JEV_RECORD_FIXTURES=1 go test -tags live ./decide/backends/jev -run TestLive -v
//
// against jev-latest (= jev-1.13.0 on 2026-10-16) for testRequest() /
// noulRequest() (fixture_request_test.go). The key travels only in the
// Authorization header; the recorder refuses to write a body containing it.
// Failure cases are derived from the recorded bodies by mutation, so no
// fixture is hand-written.
//
// When the request encoding changes on purpose (criteria wording, state
// shape), re-record: TestContract_Request fails until the request fixture
// matches encodeRequest again.

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/joustmania/agent/decide"
)

const fixtureModel = "jev-latest"

func loadFixture(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("fixture %s: %v", name, err)
	}
	return b
}

func jsonEqual(t *testing.T, got []byte, fixture string) {
	t.Helper()
	var g, w any
	if err := json.Unmarshal(got, &g); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(loadFixture(t, fixture), &w); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(g, w) {
		t.Errorf("encoded request does not match testdata/%s (re-record with JEV_RECORD_FIXTURES=1):\n%s", fixture, got)
	}
}

// TestContract_Request: encodeRequest reproduces the recorded request bodies
// exactly (as JSON values), and the choice criteria keep Question.Options order
// on the wire (Jev 1.13 leans toward the first option).
func TestContract_Request(t *testing.T) {
	out, err := encodeRequest(fixtureModel, testRequest())
	if err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, out.Body, "request_ffa.json")
	if out.Content.User != string(out.Body) {
		t.Error("content capture must record the request body")
	}
	if strings.Contains(string(out.Body), "CONTEXT-SENTINEL") || strings.Contains(string(out.Body), testRequest().State.Digest) {
		t.Error("request leaked Request.Context or the digest")
	}
	raw := string(out.Body)
	for _, q := range testRequest().Questions {
		if q.Kind != decide.KindChoice {
			continue
		}
		last := -1
		for _, o := range q.Options {
			i := strings.Index(raw, `"`+o+`":`)
			if i < last {
				t.Errorf("%s: option %q out of order on the wire", q.ID, o)
			}
			last = i
		}
	}

	out, err = encodeRequest(fixtureModel, noulRequest())
	if err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, out.Body, "request_noul.json")
}

// responseContract is the expected outcome of replaying one response body.
type responseContract struct {
	name      string
	body      func(t *testing.T) []byte
	req       func() decide.Request
	errType   string // "" = success
	isInvalid bool
	check     func(t *testing.T, res wireResult)
}

func fixture(name string) func(t *testing.T) []byte {
	return func(t *testing.T) []byte { return loadFixture(t, name) }
}

// mutated returns response_ok.json with fn applied to its decoded form.
func mutated(fn func(m map[string]any)) func(t *testing.T) []byte {
	return func(t *testing.T) []byte {
		var m map[string]any
		if err := json.Unmarshal(loadFixture(t, "response_ok.json"), &m); err != nil {
			t.Fatal(err)
		}
		fn(m)
		b, _ := json.Marshal(m)
		return b
	}
}

func answer(m map[string]any, id string) map[string]any {
	return m["answers"].(map[string]any)[id].(map[string]any)
}

var responseContracts = []responseContract{
	{name: "ok", body: fixture("response_ok.json"), req: testRequest, check: func(t *testing.T, res wireResult) {
		r := res.Response
		if r.Backend != Name {
			t.Errorf("backend = %q", r.Backend)
		}
		iv, _ := r.Find(decide.QuestionFFAIntervention)
		// Acting confidence (p(noop) for a noop choice), not Jev's native 0.75.
		if iv.Choice != decide.OptionNoop || iv.Probabilities["noop"] != 0.77 || iv.Confidence != 0.77 {
			t.Errorf("intervention = %+v", iv)
		}
		if a, _ := r.Find(decide.QuestionFFATarget); a.Choice != "MOCK0003" || a.Confidence != 0.17 || a.Probabilities["none"] != 0.38 {
			t.Errorf("target = %+v", a)
		}
		// Jev score 2.19 on levels 0..4 -> 2.19/4 on [0,1].
		if a, _ := r.Find(decide.QuestionFFAMagnitude); math.Abs(a.Score-2.19/4) > 1e-12 || a.Confidence != 0.13 || a.Probabilities != nil {
			t.Errorf("magnitude = %+v", a)
		}
		want := map[string]float64{"ffa.intervention": 0.75, "ffa.target": 0.17, "ffa.magnitude": 0.13}
		if !reflect.DeepEqual(res.NativeConfidence, want) {
			t.Errorf("native confidence = %v", res.NativeConfidence)
		}
		if res.Model != "jev-1.13.0" || res.Usage == nil || res.Usage.InputTokens != 2184 || res.Usage.OutputTokens != 225 {
			t.Errorf("model/usage = %q/%+v", res.Model, res.Usage)
		}
		if !strings.Contains(res.Text, `"ffa.intervention"`) {
			t.Errorf("text = %q", res.Text)
		}
	}},
	{name: "noul", body: fixture("response_noul.json"), req: noulRequest, check: func(t *testing.T, res wireResult) {
		a, _ := res.Response.Find("about_to_end")
		if a.Noul == nil || *a.Noul != 0.54 || math.Abs(a.Confidence-0.08) > 1e-12 {
			t.Errorf("about_to_end = %+v", a)
		}
		a, _ = res.Response.Find("lopsided")
		if a.Noul == nil || *a.Noul != 0.4 || math.Abs(a.Confidence-0.2) > 1e-12 {
			t.Errorf("lopsided = %+v", a)
		}
		if len(res.NativeConfidence) != 0 {
			t.Errorf("a noul has no native confidence: %v", res.NativeConfidence)
		}
	}},
	{name: "missing answer", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { delete(m["answers"].(map[string]any), "ffa.target") })},
	{name: "choice not an option", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.intervention")["choice"] = "launch_rockets" })},
	{name: "choice not the argmax", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.intervention")["choice"] = "grant_shield" })},
	{name: "probabilities far from 1", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.intervention")["probabilities"].(map[string]any)["noop"] = 0.5 })},
	{name: "score above the ladder", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.magnitude")["score"] = 4.5 })},
	{name: "confidence above 1", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.target")["confidence"] = 1.5 })},
	{name: "wrong answer type", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { answer(m, "ffa.magnitude")["type"] = "noul" })},
	{name: "missing confidence", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) { delete(answer(m, "ffa.target"), "confidence") })},
	// Review probe (#1261): options missing from probabilities must not read as
	// p=0 (a missing noop would make the acting confidence a false 1.0).
	{name: "probabilities miss options", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) {
			a := answer(m, "ffa.intervention")
			a["choice"] = "grant_shield"
			a["confidence"] = 0.1
			a["probabilities"] = map[string]any{"grant_shield": 0.5, "eliminate_player": 0.46}
		})},
	{name: "probabilities miss noop only", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) {
			delete(answer(m, "ffa.intervention")["probabilities"].(map[string]any), "noop")
		})},
	{name: "probability for an unknown option", req: testRequest, errType: "invalid_output", isInvalid: true,
		body: mutated(func(m map[string]any) {
			p := answer(m, "ffa.target")["probabilities"].(map[string]any)
			delete(p, "MOCK0002")
			p["MOCK9999"] = 0.02
		})},
	{name: "no answers", req: testRequest, errType: "no_answers", isInvalid: true,
		body: mutated(func(m map[string]any) { m["answers"] = map[string]any{} })},
	{name: "noul out of range", req: noulRequest, errType: "invalid_output", isInvalid: true,
		body: func(t *testing.T) []byte {
			return []byte(strings.Replace(string(loadFixture(t, "response_noul.json")), `"noul": 0.54`, `"noul": 1.54`, 1))
		}},
}

// TestContract_Responses replays every response through decodeResponse.
func TestContract_Responses(t *testing.T) {
	for _, c := range responseContracts {
		t.Run(c.name, func(t *testing.T) {
			res, errType, err := decodeResponse(c.req(), c.body(t))
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
				if verr := decide.Validate(c.req(), res.Response); verr != nil {
					t.Errorf("Validate: %v", verr)
				}
			}
			if c.check != nil {
				c.check(t, res)
			}
		})
	}
}

// TestContract_DecideReplay serves every response from an httptest server and
// runs the full Decide path, so the fixtures exercise jev.go too.
func TestContract_DecideReplay(t *testing.T) {
	for _, c := range responseContracts {
		t.Run(c.name, func(t *testing.T) {
			api := newFakeAPI(t, 200, c.body(t), 0)
			resp, err := api.backend(fixtureModel, true).Decide(context.Background(), c.req())
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

// TestContract_ErrorEnvelopes replays the recorded 401 and 422 bodies: the
// status becomes error.type, the error is not ErrInvalid (it is a transport /
// configuration failure, fallback_reason=error), and the message never
// carries the Authorization header.
func TestContract_ErrorEnvelopes(t *testing.T) {
	for _, c := range []struct {
		fixture string
		status  int
	}{{"response_401.json", 401}, {"response_422.json", 422}} {
		t.Run(c.fixture, func(t *testing.T) {
			api := newFakeAPI(t, c.status, loadFixture(t, c.fixture), 0)
			_, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
			if err == nil || errors.Is(err, decide.ErrInvalid) {
				t.Fatalf("err = %v", err)
			}
			if strings.Contains(err.Error(), "jev-test-key") {
				t.Error("error leaks the API key")
			}
			if got := attrs(api.lastSpan(t))["error.type"].AsString(); got != strings.TrimSuffix(strings.TrimPrefix(c.fixture, "response_"), ".json") {
				t.Errorf("error.type = %q", got)
			}
		})
	}
}
