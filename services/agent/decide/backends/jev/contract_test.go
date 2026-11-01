package jev

// Contract test: replays the fixtures in testdata/ through wire.go (and, for
// the responses, through the whole two-stage Decide path against an httptest
// server).
//
// The fixtures are REAL Jev traffic, recorded with
//
//	JEV_API_KEY=... JEV_RECORD_FIXTURES=1 go test -tags live ./decide/backends/jev -run 'TestLiveDecide$|TestLiveDetails$' -v
//
// against jev-latest (= jev-1.13.0 on 2026-10-16) for the near-threshold
// state (fixture_request_test.go):
//
//	gate_request_intervention.json / gate_response_intervention.json
//	    stage 1 of testRequest(): the gate noul and ffa.intervention without
//	    noop (#1281); Jev answered p_yes 0.70 and adjust_player_sensitivity
//	gate_request_details.json / gate_response_details.json
//	    the stage 2 that followed (target + both strength half-ladders)
//	group_request_details.json / group_response_details.json
//	    the details stage after grant_group_shield (#2467 PR 6; derived, see contract_group_uc040_test.go)
//	shield_request_details.json / shield_response_details.json
//	    stage 2 of testRequest() conditioned on grant_shield
//	balance_request_details.json / balance_response_details.json
//	    stage 2 conditioned on adjust_player_sensitivity (TestLiveDetails)
//	request_noul.json / response_noul.json   a single-stage noul request
//
// The key travels only in the Authorization header; the recorder refuses to
// write a body containing it. Failure cases and the acting first stage are
// derived from the recorded bodies by mutation, so no fixture is hand-written.
//
// When the request encoding changes on purpose (criteria wording, state
// shape), re-record: TestContract_Request fails until the request fixtures
// match encodeRequest again.

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel"

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

// firstStage is stage 1 of testRequest().
func firstStage() decide.Request {
	first := splitStages(testRequest()).first
	return first
}

// shieldDetails is stage 2 of testRequest() after grant_shield.
func shieldDetails() decide.Request { return detailsRequest(testRequest(), "grant_shield").ask }

// TestContract_Request: encodeRequest reproduces the recorded request bodies
// exactly (as JSON values), and the choice criteria keep Question.Options order
// on the wire (Jev 1.13 leans toward the first option).
func TestContract_Request(t *testing.T) {
	out, err := encodeRequest(fixtureModel, firstStage(), "")
	if err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, out.Body, "gate_request_intervention.json")
	var wire jevRequest
	if err := json.Unmarshal(out.Body, &wire); err != nil {
		t.Fatal(err)
	}
	gate, ok := wire.Questions[GateQuestionID]
	if !ok || gate.Type != typeNoul || gate.Instructions != gateInstructions {
		t.Errorf("stage 1 must ask the gate noul: %+v", gate)
	}
	if c, _ := gate.Criteria.(map[string]any); c["true"] == nil || c["false"] == nil {
		t.Errorf("gate criteria = %v, want true and false descriptions", gate.Criteria)
	}
	if iv := wire.Questions[decide.QuestionFFAIntervention]; iv.Type != typeChoice || iv.Instructions != gatedInterventionInstructions {
		t.Errorf("stage-1 intervention = %+v", iv)
	}
	if c, _ := wire.Questions[decide.QuestionFFAIntervention].Criteria.(map[string]any); c[decide.OptionNoop] != nil || len(c) != len(fullInterventions) {
		t.Errorf("stage-1 intervention options = %v, want every allowed intervention and no noop", c)
	}
	if strings.Contains(wire.State.Game, "Doing nothing is often right") {
		t.Error("the game description must not bias the gate toward no (#1281)")
	}
	if out.Content.User != string(out.Body) {
		t.Error("content capture must record the request body")
	}
	raw := string(out.Body)
	if strings.Contains(raw, "CONTEXT-SENTINEL") || strings.Contains(raw, testRequest().State.Digest) {
		t.Error("request leaked Request.Context or the digest")
	}
	if strings.Contains(raw, `"ffa.target"`) || strings.Contains(raw, `"ffa.magnitude"`) {
		t.Error("stage 1 must ask only the gate and the intervention")
	}
	for _, q := range firstStage().Questions {
		last := -1
		for _, o := range q.Options {
			i := strings.Index(raw, `"`+o+`":`)
			if i < last {
				t.Errorf("%s: option %q out of order on the wire", q.ID, o)
			}
			last = i
		}
	}

	out, err = encodeRequest(fixtureModel, shieldDetails(), "grant_shield")
	if err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, out.Body, "shield_request_details.json")

	out, err = encodeRequest(fixtureModel, noulRequest(), "")
	if err != nil {
		t.Fatal(err)
	}
	jsonEqual(t, out.Body, "request_noul.json")
}

// responseContract is the expected outcome of decoding one response body.
type responseContract struct {
	name      string
	body      func(t *testing.T) []byte
	req       func() decide.Request
	chosen    string
	errType   string // "" = success
	isInvalid bool
	check     func(t *testing.T, res wireResult)
}

func fixture(name string) func(t *testing.T) []byte {
	return func(t *testing.T) []byte { return loadFixture(t, name) }
}

// mutate returns fixture name with fn applied to its decoded form.
func mutate(t *testing.T, name string, fn func(m map[string]any)) []byte {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(loadFixture(t, name), &m); err != nil {
		t.Fatal(err)
	}
	fn(m)
	b, _ := json.Marshal(m)
	return b
}

func mutated(name string, fn func(m map[string]any)) func(t *testing.T) []byte {
	return func(t *testing.T) []byte { return mutate(t, name, fn) }
}

func answer(m map[string]any, id string) map[string]any {
	return m["answers"].(map[string]any)[id].(map[string]any)
}

const (
	fxIntervention = "gate_response_intervention.json"
	fxGateDetails  = "gate_response_details.json"
	fxDetails      = "shield_response_details.json"
)

// shieldLevels sets the shield details body's magnitude level probabilities
// (levels 0..4 = 3, 4.75, 6.5, 8.25, 10 s) and expected score.
func shieldLevels(score float64, probs ...float64) func(m map[string]any) {
	return func(m map[string]any) {
		a := answer(m, "ffa.magnitude")
		a["score"] = score
		if probs == nil {
			delete(a, "probabilities")
			return
		}
		p := map[string]any{}
		for i, v := range probs {
			p[strconv.Itoa(i)] = v
		}
		a["probabilities"] = p
	}
}

// shieldSeconds checks the decoded shield magnitude resolves to want seconds.
func shieldSeconds(want float64) func(t *testing.T, res wireResult) {
	return func(t *testing.T, res wireResult) {
		a, _ := res.Response.Find(decide.QuestionFFAMagnitude)
		if v, _ := decide.ResolveMagnitude("grant_shield", a.Score); v != want {
			t.Errorf("shield = %v s, want %v s (%+v)", v, want, a)
		}
	}
}

var responseContracts = []responseContract{
	{name: "intervention", body: fixture(fxIntervention), req: firstStage, check: func(t *testing.T, res wireResult) {
		r := res.Response
		if r.Backend != Name || len(r.Answers) != 2 {
			t.Fatalf("response = %+v", r)
		}
		// The raw stage-1 answers: Jev's pick among the interventions (noop is
		// not an option) and the gate. Decide synthesises the routing answer.
		iv, _ := r.Find(decide.QuestionFFAIntervention)
		if iv.Choice != "adjust_player_sensitivity" || iv.Probabilities["adjust_player_sensitivity"] != 0.38 || len(iv.Probabilities) != len(fullInterventions) {
			t.Errorf("intervention = %+v", iv)
		}
		if g, _ := r.Find(GateQuestionID); g.Noul == nil || *g.Noul != 0.7 {
			t.Errorf("gate = %+v", g)
		}
		if !reflect.DeepEqual(res.NativeConfidence, map[string]float64{"ffa.intervention": 0.32}) {
			t.Errorf("native confidence = %v", res.NativeConfidence)
		}
		if res.Model != "jev-1.13.0" || res.Usage == nil || res.Usage.InputTokens != 1622 || res.Usage.OutputTokens != 163 {
			t.Errorf("model/usage = %q/%+v", res.Model, res.Usage)
		}
		if !strings.Contains(res.Text, `"jev.gate"`) {
			t.Errorf("text = %q", res.Text)
		}
	}},
	{name: "details", body: fixture(fxDetails), req: shieldDetails, chosen: "grant_shield", check: func(t *testing.T, res wireResult) {
		r := res.Response
		if a, _ := r.Find(decide.QuestionFFATarget); a.Choice != "MOCK0003" || a.Confidence != 0.98 || a.Probabilities["MOCK0003"] != 0.99 {
			t.Errorf("target = %+v", a)
		}
		// Level probabilities 0.19 / 0.24 / 0.39 / 0.15 / 0.03: the most
		// likely level is 2 of grant_shield's own ladder (3, 4.75, 6.5, 8.25,
		// 10 s): 6.5 s, normalized 0.5.
		a, _ := r.Find(decide.QuestionFFAMagnitude)
		if v, _ := decide.ResolveMagnitude("grant_shield", a.Score); a.Score != 0.5 || v != 6.5 || a.Confidence != 0.31 {
			t.Errorf("magnitude = %+v (%v s)", a, v)
		}
		want := map[string]float64{"ffa.target": 0.98, "ffa.magnitude": 0.31}
		if !reflect.DeepEqual(res.NativeConfidence, want) {
			t.Errorf("native confidence = %v", res.NativeConfidence)
		}
	}},
	// #1281: the magnitude is the MOST LIKELY level, not the rounded expected
	// score. The #1265 fixture's split (0.29 / 0.28 / 0.30, score 1.3) rounded
	// to level 1 although level 2 is the mode.
	{name: "magnitude is the most likely level, not round(score)", req: shieldDetails, chosen: "grant_shield",
		body:  mutated(fxDetails, shieldLevels(1.3, 0.29, 0.28, 0.30, 0.10, 0.03)),
		check: shieldSeconds(6.5)},
	{name: "magnitude tie goes to the gentler level", req: shieldDetails, chosen: "grant_shield",
		body:  mutated(fxDetails, shieldLevels(2.4, 0.05, 0.10, 0.35, 0.35, 0.15)),
		check: shieldSeconds(6.5)},
	{name: "magnitude without level probabilities rounds the score", req: shieldDetails, chosen: "grant_shield",
		body:  mutated(fxDetails, shieldLevels(1.3)),
		check: shieldSeconds(4.75)},
	{name: "magnitude level probabilities miss a level", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, shieldLevels(1.3, 0.3, 0.3, 0.4))},
	{name: "magnitude level probabilities all zero", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, shieldLevels(1.3, 0, 0, 0, 0, 0))},
	{name: "magnitude level probability negative", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, shieldLevels(1.3, 0.5, -0.1, 0.3, 0.2, 0.1))},
	{name: "noul", body: fixture("response_noul.json"), req: noulRequest, check: func(t *testing.T, res wireResult) {
		a, _ := res.Response.Find("about_to_end")
		if a.Noul == nil || *a.Noul != 0.6 || math.Abs(a.Confidence-0.2) > 1e-12 {
			t.Errorf("about_to_end = %+v", a)
		}
		a, _ = res.Response.Find("lopsided")
		if a.Noul == nil || *a.Noul != 0.31 || math.Abs(a.Confidence-0.38) > 1e-12 {
			t.Errorf("lopsided = %+v", a)
		}
		if len(res.NativeConfidence) != 0 {
			t.Errorf("a noul has no native confidence: %v", res.NativeConfidence)
		}
	}},
	{name: "missing answer", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			m["answers"].(map[string]any)["other"] = answer(m, "ffa.intervention")
			delete(m["answers"].(map[string]any), "ffa.intervention")
		})},
	{name: "choice not an option", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { answer(m, "ffa.intervention")["choice"] = "launch_rockets" })},
	{name: "choice not the argmax", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { answer(m, "ffa.intervention")["choice"] = "grant_shield" })},
	{name: "probabilities far from 1", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			answer(m, "ffa.intervention")["probabilities"].(map[string]any)["grant_shield"] = 0.0
		})},
	{name: "missing confidence", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { delete(answer(m, "ffa.intervention"), "confidence") })},
	// Review probes (#1261): options missing from probabilities must not read
	// as p=0, in either stage; the gated stage 1 has no noop option, so a noop
	// probability is an unknown option.
	{name: "probabilities miss options", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			a := answer(m, "ffa.intervention")
			a["choice"] = "grant_shield"
			a["confidence"] = 0.1
			a["probabilities"] = map[string]any{"grant_shield": 0.5, "eliminate_player": 0.46}
		})},
	{name: "stage-1 probabilities include noop", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			answer(m, "ffa.intervention")["probabilities"].(map[string]any)["noop"] = 0.0
		})},
	{name: "gate missing", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { delete(m["answers"].(map[string]any), GateQuestionID) })},
	{name: "gate out of range", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { answer(m, GateQuestionID)["noul"] = 1.2 })},
	{name: "gate wrong answer type", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { answer(m, GateQuestionID)["type"] = "choice" })},
	{name: "details probabilities miss a player", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) {
			delete(answer(m, "ffa.target")["probabilities"].(map[string]any), "MOCK0002")
		})},
	{name: "details probability for an unknown option", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) {
			p := answer(m, "ffa.target")["probabilities"].(map[string]any)
			delete(p, "MOCK0002")
			p["none"] = 0.0
		})},
	{name: "no answers", req: firstStage, errType: "no_answers", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { m["answers"] = map[string]any{} })},
	{name: "details score above the ladder", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) { answer(m, "ffa.magnitude")["score"] = 4.5 })},
	{name: "details target none", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) { answer(m, "ffa.target")["choice"] = "none" })},
	{name: "details confidence above 1", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) { answer(m, "ffa.target")["confidence"] = 1.5 })},
	{name: "details wrong answer type", req: shieldDetails, chosen: "grant_shield", errType: "invalid_output", isInvalid: true,
		body: mutated(fxDetails, func(m map[string]any) { answer(m, "ffa.magnitude")["type"] = "noul" })},
	{name: "noul out of range", req: noulRequest, errType: "invalid_output", isInvalid: true,
		body: func(t *testing.T) []byte {
			return []byte(strings.Replace(string(loadFixture(t, "response_noul.json")), `"noul": 0.6`, `"noul": 1.6`, 1))
		}},
}

// TestContract_Responses decodes every response for its stage. The "noul" case
// checks Jev's native noul answer and its confidence |2p-1| (UC-040 A12).
// UC: UC-040 A12
func TestContract_Responses(t *testing.T) {
	for _, c := range responseContracts {
		t.Run(c.name, func(t *testing.T) {
			res, errType, err := decodeResponse(c.req(), c.chosen, c.body(t))
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

// gatedStage1 is the recorded stage-1 body with the gate set to pYes and,
// unless iv is "", iv made Jev's most probable intervention (probabilities
// swapped with the recorded pick, so every option stays covered).
func gatedStage1(t *testing.T, iv string, pYes float64) []byte {
	return mutate(t, fxIntervention, func(m map[string]any) {
		answer(m, GateQuestionID)["noul"] = pYes
		if iv == "" {
			return
		}
		a := answer(m, "ffa.intervention")
		probs := a["probabilities"].(map[string]any)
		if _, ok := probs[iv]; !ok {
			probs[iv] = 0.0 // an option outside the recorded allow-list
		}
		chosen := a["choice"].(string)
		probs[iv], probs[chosen] = probs[chosen], probs[iv]
		a["choice"] = iv
	})
}

// actingIntervention is a stage-1 body whose gate opens (p_yes 0.9) on iv.
func actingIntervention(t *testing.T, iv string) []byte { return gatedStage1(t, iv, 0.9) }

// noopStage1 is the recorded stage-1 body with the gate closed (p_yes 0.3).
func noopStage1(t *testing.T) []byte { return gatedStage1(t, "", 0.3) }

func ok(body []byte) fakeReply { return fakeReply{status: 200, body: body} }

// checkRouting asserts the synthesised ffa.intervention answer (#1281):
// {noop: 1-p_yes, best: p_yes, every other option: 0}, every option of the
// original question covered, the acting confidence p_yes (1-p_yes for noop).
func checkRouting(t *testing.T, iv decide.Answer, best string, pYes float64) {
	t.Helper()
	want := map[string]float64{}
	for _, o := range testRequest().Questions[0].Options {
		want[o] = 0
	}
	want[decide.OptionNoop], want[best] = 1-pYes, pYes
	if len(iv.Probabilities) != len(want) {
		t.Errorf("routing distribution covers %d options, want %d", len(iv.Probabilities), len(want))
	}
	for o, p := range want {
		if math.Abs(iv.Probabilities[o]-p) > 1e-12 {
			t.Errorf("p(%s) = %v, want %v", o, iv.Probabilities[o], p)
		}
	}
	wantChoice, wantConf := decide.OptionNoop, 1-pYes
	if pYes > 0.5 {
		wantChoice, wantConf = best, pYes
	}
	if iv.Choice != wantChoice || math.Abs(iv.Confidence-wantConf) > 1e-12 {
		t.Errorf("routing answer = %s at %v, want %s at %v", iv.Choice, iv.Confidence, wantChoice, wantConf)
	}
}

// TestContract_NoopRound: the gate says no (p_yes 0.3 <= 0.5), so Decide makes
// ONE call, answers noop at confidence 0.7 and target none / magnitude at the
// low end (the rules adapter's noop convention), every option covered. Jev's
// real stage-1 answers are on the span.
// UC: UC-040 A13
func TestContract_NoopRound(t *testing.T) {
	api := newScriptedAPI(t, ok(noopStage1(t)))
	resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
	if err != nil {
		t.Fatal(err)
	}
	if n := api.calls.Load(); n != 1 {
		t.Fatalf("%d calls, want 1 (gate no: no details stage)", n)
	}
	if err := decide.Validate(testRequest(), resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Answers) != 3 {
		t.Errorf("the gate answer leaked: %+v", resp.Answers)
	}
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	checkRouting(t, iv, "adjust_player_sensitivity", 0.3)
	tg, _ := resp.Find(decide.QuestionFFATarget)
	if tg.Choice != decide.TargetNone || tg.Confidence != 1 || len(tg.Probabilities) != 4 || tg.Probabilities[decide.TargetNone] != 1 {
		t.Errorf("target = %+v", tg)
	}
	if m, _ := resp.Find(decide.QuestionFFAMagnitude); m.Score != 0 || math.Abs(m.Confidence-0.7) > 1e-12 {
		t.Errorf("magnitude = %+v", m)
	}
	if want := "jev: noop (gate p_yes=0.30; adjust_player_sensitivity p=0.38 among interventions)"; resp.Rationale != want {
		t.Errorf("rationale = %q, want %q", resp.Rationale, want)
	}
	spans := api.chatSpans()
	if len(spans) != 1 {
		t.Fatalf("spans = %d", len(spans))
	}
	a := attrs(spans[0])
	if a[AttrStage].AsString() != StageIntervention || a[AttrGatePYes].AsFloat64() != 0.3 || a[AttrStage1Choice].AsString() != "adjust_player_sensitivity" {
		t.Errorf("stage-1 span: stage %q p_yes %v choice %q", a[AttrStage].AsString(), a[AttrGatePYes].AsFloat64(), a[AttrStage1Choice].AsString())
	}
	var dist map[string]float64
	if err := json.Unmarshal([]byte(a[AttrStage1Distribution].AsString()), &dist); err != nil || dist["adjust_player_sensitivity"] != 0.38 || dist["grant_shield"] != 0.3 {
		t.Errorf("jev.stage1.distribution = %q (%v)", a[AttrStage1Distribution].AsString(), err)
	}
	if _, has := dist[decide.OptionNoop]; has || len(dist) != len(fullInterventions) {
		t.Errorf("jev.stage1.distribution must be Jev's real answer over the interventions: %v", dist)
	}
}

// TestContract_ActRound replays the recorded act round: the gate says yes
// (p_yes 0.70) and Jev picks adjust_player_sensitivity, so stage 2 asks target
// and strength CONDITIONED on it (alive players only, the lever's half-ladders).
// Two chat spans under the caller's span, one per stage. The grant_shield
// subtest replays the shield details fixture after a mutated stage 1.
// UC: UC-040 A13
func TestContract_ActRound(t *testing.T) {
	api := newScriptedAPI(t, ok(loadFixture(t, fxIntervention)), ok(loadFixture(t, fxGateDetails)))
	ctx, parent := otel.Tracer("test").Start(context.Background(), "agent.decide")
	resp, err := api.backend(fixtureModel, false).Decide(ctx, testRequest())
	parent.End()
	if err != nil {
		t.Fatal(err)
	}
	if n := api.calls.Load(); n != 2 {
		t.Fatalf("%d calls, want 2", n)
	}
	if err := decide.Validate(testRequest(), resp); err != nil {
		t.Fatal(err)
	}
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	tg, _ := resp.Find(decide.QuestionFFATarget)
	mg, _ := resp.Find(decide.QuestionFFAMagnitude)
	checkRouting(t, iv, "adjust_player_sensitivity", 0.7)
	if tg.Choice != "MOCK0003" || len(tg.Probabilities) != 4 || tg.Probabilities[decide.TargetNone] != 0 {
		t.Errorf("target = %+v (every option, none included, must be covered)", tg)
	}
	// MOCK0003 struggles: the help side; its most likely strength is level 1
	// (0.13 / 0.68 / 0.19): 0.8.
	if v, _ := decide.ResolveMagnitude("adjust_player_sensitivity", mg.Score); v != 0.8 {
		t.Errorf("magnitude = %+v -> %v", mg, v)
	}
	want := "jev: adjust_player_sensitivity (gate p_yes=0.70; adjust_player_sensitivity p=0.38 among interventions) on MOCK0003 (p=0.89)"
	if resp.Rationale != want {
		t.Errorf("rationale = %q, want %q", resp.Rationale, want)
	}

	api.mu.Lock()
	second := api.bodies[1]
	api.mu.Unlock()
	jsonEqual(t, second, "gate_request_details.json")

	spans := api.chatSpans()
	if len(spans) != 2 {
		t.Fatalf("%d chat spans, want 2", len(spans))
	}
	for i, stage := range []string{StageIntervention, StageDetails} {
		if got := attrs(spans[i])[AttrStage].AsString(); got != stage {
			t.Errorf("span %d stage = %q, want %q", i, got, stage)
		}
		if spans[i].Parent().SpanID() != parent.SpanContext().SpanID() {
			t.Errorf("span %d is not a child of agent.decide", i)
		}
	}
	if a := attrs(spans[0]); a[AttrGatePYes].AsFloat64() != 0.7 {
		t.Errorf("jev.gate.p_yes = %v", a[AttrGatePYes].AsFloat64())
	}
	if _, has := attrs(spans[1])[AttrGatePYes]; has {
		t.Error("the details span carries no gate attributes")
	}

	t.Run("grant_shield", func(t *testing.T) {
		api := newScriptedAPI(t, ok(actingIntervention(t, "grant_shield")), ok(loadFixture(t, fxDetails)))
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
		if err != nil {
			t.Fatal(err)
		}
		iv, _ := resp.Find(decide.QuestionFFAIntervention)
		checkRouting(t, iv, "grant_shield", 0.9)
		mg, _ := resp.Find(decide.QuestionFFAMagnitude)
		if v, _ := decide.ResolveMagnitude("grant_shield", mg.Score); v != 6.5 {
			t.Errorf("shield = %v s", v)
		}
		api.mu.Lock()
		second := api.bodies[1]
		api.mu.Unlock()
		jsonEqual(t, second, "shield_request_details.json")
	})
}

// TestContract_GateThreshold: the round acts iff p_yes > 0.5 (decide.Validate's
// argmax rule on the synthesised distribution); a closed gate makes no details
// call. The acting confidence is p_yes, which the decision hook then compares
// with decision.jev.act_confidence_threshold (#1287).
// Spec: decide-contract.md §3.1, UC-040 A13, FR-047.
// UC: UC-040 A13
func TestContract_GateThreshold(t *testing.T) {
	for _, c := range []struct {
		pYes  float64
		calls int32
	}{{0, 1}, {0.3, 1}, {0.5, 1}, {0.51, 2}, {0.7, 2}, {1, 2}} {
		api := newScriptedAPI(t, ok(gatedStage1(t, "", c.pYes)), ok(loadFixture(t, fxGateDetails)))
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
		if err != nil {
			t.Fatalf("p_yes %v: %v", c.pYes, err)
		}
		if n := api.calls.Load(); n != c.calls {
			t.Errorf("p_yes %v: %d calls, want %d", c.pYes, n, c.calls)
		}
		iv, _ := resp.Find(decide.QuestionFFAIntervention)
		checkRouting(t, iv, "adjust_player_sensitivity", c.pYes)
	}
}

// TestContract_SessionInterventions: a session-scoped intervention takes
// target none without asking; with numeric bounds only the magnitude is asked,
// without them no details call is made at all.
func TestContract_SessionInterventions(t *testing.T) {
	t.Run("with bounds", func(t *testing.T) {
		details := mutate(t, fxDetails, func(m map[string]any) { delete(m["answers"].(map[string]any), "ffa.target") })
		api := newScriptedAPI(t, ok(actingIntervention(t, "adjust_music_tempo")), ok(details))
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
		if err != nil {
			t.Fatal(err)
		}
		api.mu.Lock()
		second := string(api.bodies[1])
		api.mu.Unlock()
		if strings.Contains(second, `"ffa.target"`) || !strings.Contains(second, `"chosen_intervention":"adjust_music_tempo"`) {
			t.Errorf("details request = %s", second)
		}
		tg, _ := resp.Find(decide.QuestionFFATarget)
		mg, _ := resp.Find(decide.QuestionFFAMagnitude)
		if tg.Choice != decide.TargetNone || tg.Confidence != 1 {
			t.Errorf("target = %+v", tg)
		}
		// The shield fixture's most likely level is 2: tempo 1.15.
		if v, _ := decide.ResolveMagnitude("adjust_music_tempo", mg.Score); v != 1.15 {
			t.Errorf("tempo = %v (level 2 of 1.0..1.3)", v)
		}
	})
	t.Run("without bounds", func(t *testing.T) {
		api := newScriptedAPI(t, ok(actingIntervention(t, "play_audio_cue")))
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
		if err != nil {
			t.Fatal(err)
		}
		if n := api.calls.Load(); n != 1 {
			t.Errorf("%d calls, want 1", n)
		}
		if tg, _ := resp.Find(decide.QuestionFFATarget); tg.Choice != decide.TargetNone {
			t.Errorf("target = %+v", tg)
		}
	})
}

// TestContract_DetailsFailure: a failed stage 2 fails the round (the caller
// falls back to rules) instead of returning the intervention without its
// target or magnitude. Transport failures stay non-ErrInvalid
// (fallback_reason=error), invalid answers wrap ErrInvalid (invalid_answer).
// UC: UC-040 A13
func TestContract_DetailsFailure(t *testing.T) {
	for _, tc := range []struct {
		name      string
		reply     func(t *testing.T) fakeReply
		errType   string
		isInvalid bool
	}{
		{"server error", func(*testing.T) fakeReply { return fakeReply{status: 500, body: []byte(`{"detail":"boom"}`)} }, "500", false},
		{"invalid target", func(t *testing.T) fakeReply {
			return ok(mutate(t, fxDetails, func(m map[string]any) { answer(m, "ffa.target")["choice"] = "none" }))
		}, "invalid_output", true},
		{"missing magnitude", func(t *testing.T) fakeReply {
			return ok(mutate(t, fxDetails, func(m map[string]any) { delete(m["answers"].(map[string]any), "ffa.magnitude") }))
		}, "invalid_output", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			api := newScriptedAPI(t, ok(actingIntervention(t, "grant_shield")), tc.reply(t))
			resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
			if err == nil {
				t.Fatalf("Decide = %+v, want an error", resp)
			}
			if errors.Is(err, decide.ErrInvalid) != tc.isInvalid {
				t.Errorf("errors.Is(ErrInvalid) = %v, want %v (%v)", !tc.isInvalid, tc.isInvalid, err)
			}
			if len(resp.Answers) != 0 {
				t.Error("a failed round must not return a half answer")
			}
			spans := api.chatSpans()
			if len(spans) != 2 || attrs(spans[1])["error.type"].AsString() != tc.errType || attrs(spans[0])["error.type"].AsString() != "" {
				t.Errorf("spans = %d, details error.type = %q", len(spans), attrs(spans[len(spans)-1])["error.type"].AsString())
			}
		})
	}
}

// TestContract_SharedDeadline: both stages share one Config.Timeout; a slow
// stage 2 times out at the round's deadline, not a fresh one.
// UC: UC-040 A13
func TestContract_SharedDeadline(t *testing.T) {
	api := newScriptedAPI(t,
		fakeReply{status: 200, body: actingIntervention(t, "grant_shield"), delay: 150 * time.Millisecond},
		fakeReply{status: 200, body: loadFixture(t, fxDetails), delay: 2 * time.Second})
	b := api.backend(fixtureModel, false)
	b.cfg.Timeout = 300 * time.Millisecond
	start := time.Now()
	_, err := b.Decide(context.Background(), testRequest())
	if err == nil || errors.Is(err, decide.ErrInvalid) {
		t.Fatalf("err = %v", err)
	}
	if el := time.Since(start); el > 800*time.Millisecond {
		t.Errorf("round took %v, want about the 300ms deadline", el)
	}
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("err = %v, want it to wrap context.DeadlineExceeded", err)
	}
	if got := attrs(api.lastSpan(t))["error.type"].AsString(); got != "timeout" {
		t.Errorf("error.type = %q", got)
	}
}

const fxBalance = "balance_response_details.json"

// balanceDetails returns the recorded adjust_player_sensitivity details body
// with the target choice moved to target (probabilities swapped so the choice
// stays the argmax) and both strengths peaked at level (most likely level).
func balanceDetails(t *testing.T, target string, level float64) []byte {
	return mutate(t, fxBalance, func(m map[string]any) {
		a := answer(m, "ffa.target")
		probs := a["probabilities"].(map[string]any)
		chosen := a["choice"].(string)
		probs[target], probs[chosen] = probs[chosen], probs[target]
		a["choice"] = target
		if level >= 0 {
			for _, id := range []string{"ffa.magnitude.help", "ffa.magnitude.rein_in"} {
				p := map[string]any{"0": 0.1, "1": 0.1, "2": 0.1}
				p[strconv.Itoa(int(level))] = 0.8
				answer(m, id)["score"] = level
				answer(m, id)["probabilities"] = p
			}
		}
	})
}

// TestContract_TwoWayLever (#1262 review I1): for adjust_player_sensitivity
// (0.7 protect .. 1.5 handicap) and set_player_handicap (0.75 rein in .. 1.75
// help) the direction comes from the chosen target in Go; Jev only picks the
// strength. The struggling target never gets a handicap-side value, the
// calmest never a help-side one, and the neutral 1.0 is never applied.
// UC: UC-040 A13
func TestContract_TwoWayLever(t *testing.T) {
	t.Run("recorded", func(t *testing.T) {
		api := newScriptedAPI(t, ok(actingIntervention(t, "adjust_player_sensitivity")), ok(loadFixture(t, fxBalance)))
		resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
		if err != nil {
			t.Fatal(err)
		}
		api.mu.Lock()
		second := api.bodies[1]
		api.mu.Unlock()
		jsonEqual(t, second, "balance_request_details.json")
		tg, _ := resp.Find(decide.QuestionFFATarget)
		mg, _ := resp.Find(decide.QuestionFFAMagnitude)
		// Jev's most likely help strength is level 1 (0.13 / 0.68 / 0.19): 0.8;
		// the rein-in answer is dropped.
		if v, _ := decide.ResolveMagnitude("adjust_player_sensitivity", mg.Score); tg.Choice != "MOCK0003" || v != 0.8 || mg.Confidence != 0.52 {
			t.Errorf("target %s, magnitude %+v -> %v", tg.Choice, mg, v)
		}
		if len(resp.Answers) != 3 {
			t.Errorf("internal strength answers leaked: %+v", resp.Answers)
		}
	})
	for _, iv := range []string{"adjust_player_sensitivity", "set_player_handicap"} {
		for _, target := range []string{"MOCK0003", "MOCK0002", "MOCK0004"} {
			for _, level := range []float64{0, 1, 2} {
				st := testRequest().State
				req := decide.Request{State: st, Questions: decide.FFAQuestions(st, append([]string{"set_player_handicap"}, fullInterventions...))}
				stage1 := actingIntervention(t, iv)
				var m map[string]any
				_ = json.Unmarshal(stage1, &m)
				probs := answer(m, "ffa.intervention")["probabilities"].(map[string]any)
				if _, ok := probs["set_player_handicap"]; !ok {
					probs["set_player_handicap"] = 0.0
				}
				stage1, _ = json.Marshal(m)
				api := newScriptedAPI(t, ok(stage1), ok(balanceDetails(t, target, level)))
				resp, err := api.backend(fixtureModel, false).Decide(context.Background(), req)
				if err != nil {
					t.Fatalf("%s %s %v: %v", iv, target, level, err)
				}
				mg, _ := resp.Find(decide.QuestionFFAMagnitude)
				v, _ := decide.ResolveMagnitude(iv, mg.Score)
				helped := v < 1 // sensitivity: below neutral protects
				if iv == "set_player_handicap" {
					helped = v > 1 // handicap: above neutral helps
				}
				// MOCK0003 struggles, MOCK0002 has the most headroom, MOCK0004
				// is ambiguous (help, fail-safe).
				wantHelp := target != "MOCK0002"
				if v == 1 || helped != wantHelp {
					t.Errorf("%s on %s at level %v: value %v (helped=%v, want %v)", iv, target, level, v, helped, wantHelp)
				}
			}
		}
	}
}

// TestContract_NoCandidate (#1262 review S2): a player intervention with
// bounds and no alive candidate is answered locally without a details call;
// the target none at confidence 0 is what the decision hook rejects.
func TestContract_NoCandidate(t *testing.T) {
	c := lopsidedContext()
	for _, p := range c.Players {
		p.Active = boolp(false)
	}
	st := decide.BuildState(c)
	req := decide.Request{State: st, Questions: decide.FFAQuestions(st, fullInterventions)}
	api := newScriptedAPI(t, ok(actingIntervention(t, "grant_shield")))
	resp, err := api.backend(fixtureModel, false).Decide(context.Background(), req)
	if err != nil {
		t.Fatal(err)
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("%d calls, want 1 (no candidate, no details call)", n)
	}
	if tg, _ := resp.Find(decide.QuestionFFATarget); tg.Choice != decide.TargetNone || tg.Confidence != 0 {
		t.Errorf("target = %+v", tg)
	}
}

// TestContract_DecideReplay serves every stage-1 / single-stage response from
// an httptest server and runs the full Decide path, so the fixtures exercise
// jev.go too (the stage-2 contracts are covered by the round tests above).
func TestContract_DecideReplay(t *testing.T) {
	for _, c := range responseContracts {
		if c.chosen != "" {
			continue
		}
		t.Run(c.name, func(t *testing.T) {
			req := testRequest()
			if c.name == "noul" || c.name == "noul out of range" {
				req = noulRequest()
			}
			// A gate that opens is followed by the recorded details call.
			api := newScriptedAPI(t, ok(c.body(t)), ok(loadFixture(t, fxGateDetails)))
			resp, err := api.backend(fixtureModel, true).Decide(context.Background(), req)
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
