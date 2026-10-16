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
//	noop_request_intervention.json / noop_response_intervention.json
//	    stage 1 of testRequest() (Jev chose noop, so no stage 2 followed)
//	shield_request_details.json / shield_response_details.json
//	    stage 2 of testRequest() conditioned on grant_shield
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
	first, _, _ := splitStages(testRequest())
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
	jsonEqual(t, out.Body, "noop_request_intervention.json")
	if out.Content.User != string(out.Body) {
		t.Error("content capture must record the request body")
	}
	raw := string(out.Body)
	if strings.Contains(raw, "CONTEXT-SENTINEL") || strings.Contains(raw, testRequest().State.Digest) {
		t.Error("request leaked Request.Context or the digest")
	}
	if strings.Contains(raw, `"ffa.target"`) || strings.Contains(raw, `"ffa.magnitude"`) {
		t.Error("stage 1 must ask only the intervention")
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
	fxIntervention = "noop_response_intervention.json"
	fxDetails      = "shield_response_details.json"
)

var responseContracts = []responseContract{
	{name: "intervention", body: fixture(fxIntervention), req: firstStage, check: func(t *testing.T, res wireResult) {
		r := res.Response
		if r.Backend != Name || len(r.Answers) != 1 {
			t.Fatalf("response = %+v", r)
		}
		iv, _ := r.Find(decide.QuestionFFAIntervention)
		// Acting confidence (p(noop) for a noop choice), not Jev's native 0.61.
		if iv.Choice != decide.OptionNoop || iv.Probabilities["noop"] != 0.64 || iv.Confidence != 0.64 {
			t.Errorf("intervention = %+v", iv)
		}
		if !reflect.DeepEqual(res.NativeConfidence, map[string]float64{"ffa.intervention": 0.61}) {
			t.Errorf("native confidence = %v", res.NativeConfidence)
		}
		if res.Model != "jev-1.13.0" || res.Usage == nil || res.Usage.InputTokens != 1523 || res.Usage.OutputTokens != 146 {
			t.Errorf("model/usage = %q/%+v", res.Model, res.Usage)
		}
		if !strings.Contains(res.Text, `"ffa.intervention"`) {
			t.Errorf("text = %q", res.Text)
		}
	}},
	{name: "details", body: fixture(fxDetails), req: shieldDetails, chosen: "grant_shield", check: func(t *testing.T, res wireResult) {
		r := res.Response
		if a, _ := r.Find(decide.QuestionFFATarget); a.Choice != "MOCK0003" || a.Confidence != 0.98 || a.Probabilities["MOCK0003"] != 0.99 {
			t.Errorf("target = %+v", a)
		}
		// Jev score 1.37 snaps to level 1 of grant_shield's own ladder
		// (3, 4.75, 6.5, 8.25, 10 s): 4.75 s, normalized 0.25.
		a, _ := r.Find(decide.QuestionFFAMagnitude)
		if v, _ := decide.ResolveMagnitude("grant_shield", a.Score); a.Score != 0.25 || v != 4.75 || a.Confidence != 0.17 {
			t.Errorf("magnitude = %+v (%v s)", a, v)
		}
		want := map[string]float64{"ffa.target": 0.98, "ffa.magnitude": 0.17}
		if !reflect.DeepEqual(res.NativeConfidence, want) {
			t.Errorf("native confidence = %v", res.NativeConfidence)
		}
	}},
	{name: "noul", body: fixture("response_noul.json"), req: noulRequest, check: func(t *testing.T, res wireResult) {
		a, _ := res.Response.Find("about_to_end")
		if a.Noul == nil || *a.Noul != 0.61 || math.Abs(a.Confidence-0.22) > 1e-12 {
			t.Errorf("about_to_end = %+v", a)
		}
		a, _ = res.Response.Find("lopsided")
		if a.Noul == nil || *a.Noul != 0.56 || math.Abs(a.Confidence-0.12) > 1e-12 {
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
			answer(m, "ffa.intervention")["probabilities"].(map[string]any)["noop"] = 0.3
		})},
	{name: "missing confidence", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) { delete(answer(m, "ffa.intervention"), "confidence") })},
	// Review probes (#1261): options missing from probabilities must not read
	// as p=0 (a missing noop would make the acting confidence a false 1.0), in
	// either stage.
	{name: "probabilities miss options", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			a := answer(m, "ffa.intervention")
			a["choice"] = "grant_shield"
			a["confidence"] = 0.1
			a["probabilities"] = map[string]any{"grant_shield": 0.5, "eliminate_player": 0.46}
		})},
	{name: "probabilities miss noop only", req: firstStage, errType: "invalid_output", isInvalid: true,
		body: mutated(fxIntervention, func(m map[string]any) {
			delete(answer(m, "ffa.intervention")["probabilities"].(map[string]any), "noop")
		})},
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
			return []byte(strings.Replace(string(loadFixture(t, "response_noul.json")), `"noul": 0.61`, `"noul": 1.61`, 1))
		}},
}

// TestContract_Responses decodes every response for its stage.
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

// actingIntervention is the recorded stage-1 body with iv made the most
// probable option (Jev chose noop on the recorded state).
func actingIntervention(t *testing.T, iv string) []byte {
	return mutate(t, fxIntervention, func(m map[string]any) {
		a := answer(m, "ffa.intervention")
		probs := a["probabilities"].(map[string]any)
		probs[iv], probs["noop"] = probs["noop"], probs[iv]
		a["choice"] = iv
	})
}

func ok(body []byte) fakeReply { return fakeReply{status: 200, body: body} }

// TestContract_NoopRound: stage 1 answers noop, so Decide makes ONE call and
// answers target none / magnitude at the low end (the rules adapter's noop
// convention), every option covered.
func TestContract_NoopRound(t *testing.T) {
	api := newScriptedAPI(t, ok(loadFixture(t, fxIntervention)))
	resp, err := api.backend(fixtureModel, false).Decide(context.Background(), testRequest())
	if err != nil {
		t.Fatal(err)
	}
	if n := api.calls.Load(); n != 1 {
		t.Fatalf("%d calls, want 1 (noop needs no details stage)", n)
	}
	if err := decide.Validate(testRequest(), resp); err != nil {
		t.Fatal(err)
	}
	tg, _ := resp.Find(decide.QuestionFFATarget)
	if tg.Choice != decide.TargetNone || tg.Confidence != 1 || len(tg.Probabilities) != 4 || tg.Probabilities[decide.TargetNone] != 1 {
		t.Errorf("target = %+v", tg)
	}
	if m, _ := resp.Find(decide.QuestionFFAMagnitude); m.Score != 0 || m.Confidence != 0.64 {
		t.Errorf("magnitude = %+v", m)
	}
	if resp.Rationale != "jev: noop (p=0.64)" {
		t.Errorf("rationale = %q", resp.Rationale)
	}
	spans := api.chatSpans()
	if len(spans) != 1 || attrs(spans[0])[AttrStage].AsString() != StageIntervention {
		t.Errorf("spans = %d", len(spans))
	}
}

// TestContract_ActRound: stage 1 picks grant_shield, so stage 2 asks target
// and magnitude CONDITIONED on it: the state names the intervention, the
// target options are the alive players only, the magnitude levels are the
// shield's own. Two chat spans under the caller's span, one per stage.
func TestContract_ActRound(t *testing.T) {
	api := newScriptedAPI(t, ok(actingIntervention(t, "grant_shield")), ok(loadFixture(t, fxDetails)))
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
	if iv.Choice != "grant_shield" || math.Abs(iv.Confidence-0.88) > 1e-9 {
		t.Errorf("intervention = %+v", iv)
	}
	if tg.Choice != "MOCK0003" || len(tg.Probabilities) != 4 || tg.Probabilities[decide.TargetNone] != 0 {
		t.Errorf("target = %+v (every option, none included, must be covered)", tg)
	}
	if v, _ := decide.ResolveMagnitude("grant_shield", mg.Score); v != 4.75 {
		t.Errorf("magnitude = %+v -> %v s", mg, v)
	}
	if resp.Rationale != "jev: grant_shield (p=0.64) on MOCK0003 (p=0.99)" {
		t.Errorf("rationale = %q", resp.Rationale)
	}

	api.mu.Lock()
	second := string(api.bodies[1])
	api.mu.Unlock()
	jsonEqual(t, []byte(second), "shield_request_details.json")

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
		if v, _ := decide.ResolveMagnitude("adjust_music_tempo", mg.Score); v != 1.075 {
			t.Errorf("tempo = %v (level 1 of 1.0..1.3)", v)
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
	if got := attrs(api.lastSpan(t))["error.type"].AsString(); got != "timeout" {
		t.Errorf("error.type = %q", got)
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
			api := newFakeAPI(t, 200, c.body(t), 0)
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
