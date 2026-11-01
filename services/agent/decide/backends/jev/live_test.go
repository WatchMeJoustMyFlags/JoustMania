//go:build live

package jev

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/joustmania/agent/decide"
)

// TestLiveDecide calls the real Jev API. It is opt-in twice: the `live` build
// tag and a JEV_API_KEY in the environment. Jev bills input tokens only
// (~$0.04 per million), so a run costs a fraction of a cent.
//
//	JEV_API_KEY=... go test -tags live ./decide/backends/jev -run TestLive -v
//
// JEV_LIVE_MODEL overrides the model (default DefaultModel);
// JEV_LIVE_SCENARIO=<name> runs one FFA scenario only (and no noul unless
// JEV_LIVE_NOUL=1). With
// JEV_RECORD_FIXTURES=1 the request/response bodies are written to testdata/
// (the API key travels only in the Authorization header, never in a body, and
// recordingTransport asserts that).
//
// Each scenario runs twice on a FRESH connection pool: the first round pays DNS
// + TLS setup (cold), the second is the steady-state in-game latency (warm).
// An FFA round is one call (noop) or two (act, #1262), logged per stage.
func TestLiveDecide(t *testing.T) {
	key := liveKey(t)
	for _, sc := range []struct {
		name   string
		req    decide.Request
		record string // fixture prefix of the cold round, "" = not recorded
	}{
		{"near-threshold", testRequest(), "gate_"},
		{"calm", calmRequest(), ""},
		{"dragging", draggingRequest(), ""},
		{"too-fast", tooFastRequest(), ""},
	} {
		if only := os.Getenv("JEV_LIVE_SCENARIO"); only != "" && only != sc.name {
			continue
		}
		rec := newRecordingTransport(key)
		b := liveBackend(key, rec)
		for i, label := range []string{"cold", "warm"} {
			before := rec.count()
			start := time.Now()
			resp, err := b.Decide(context.Background(), sc.req)
			elapsed := time.Since(start)
			if err != nil {
				t.Fatalf("%s %s: Decide: %v (after %v)", sc.name, label, err, elapsed)
			}
			exs := rec.since(before)
			t.Logf("%s %s: model=%s calls=%d round=%v %s", sc.name, label, b.Model(), len(exs), elapsed, resp.Rationale)
			for j, ex := range exs {
				t.Logf("  call %d: http=%v request_id=%s %s", j+1, ex.elapsed, ex.requestID, usageLine(ex.response))
			}
			for _, a := range resp.Answers {
				t.Logf("  %s: choice=%q score=%.3f confidence=%.3f probabilities=%v", a.QuestionID, a.Choice, a.Score, a.Confidence, topProbabilities(a.Probabilities, 4))
			}
			if v, ok := resolvedMagnitude(resp); ok {
				t.Logf("  magnitude resolves to %v", v)
			}
			if i == 0 && sc.record != "" {
				record(t, sc.record+"request_intervention.json", exs[0].request)
				record(t, sc.record+"response_intervention.json", exs[0].response)
				if len(exs) > 1 {
					record(t, sc.record+"request_details.json", exs[1].request)
					record(t, sc.record+"response_details.json", exs[1].response)
				}
			}
			if i == 1 && elapsed > DefaultTimeout {
				t.Errorf("steady-state latency %v exceeds the %v in-game budget", elapsed, DefaultTimeout)
			}
		}
	}

	if os.Getenv("JEV_LIVE_SCENARIO") != "" && os.Getenv("JEV_LIVE_NOUL") != "1" {
		return
	}
	// A noul request (#1229 KindNoul): single stage.
	rec := newRecordingTransport(key)
	b := liveBackend(key, rec)
	nreq := noulRequest()
	start := time.Now()
	resp, err := b.Decide(context.Background(), nreq)
	if err != nil {
		t.Fatalf("noul: %v", err)
	}
	ex := rec.last()
	t.Logf("noul: latency=%v %s", time.Since(start), usageLine(ex.response))
	for _, a := range resp.Answers {
		t.Logf("  %s: noul=%.3f confidence=%.3f", a.QuestionID, *a.Noul, a.Confidence)
	}
	record(t, "request_noul.json", ex.request)
	record(t, "response_noul.json", ex.response)
}

// TestLiveGate measures Jev's gate (#1281) on the four calibration states of
// the decide contract §3.1: one gated stage-1 call each for calm, dragging,
// near-threshold and collapsing (4 calls, no details stage). It logs p_yes and Jev's stage-1 pick per state, the
// numbers behind decision.jev.act_confidence_threshold (gate = 0.6, #1287),
// and asserts only the shape (a gated request, p_yes in [0,1], a pick), not
// the values: they move with the model and the wording.
//
//	JEV_API_KEY=... go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v
//
// The fixture states offer the full intervention list; on stage the agent
// asks with the live interventions_allowed (ambient), so the pick differs.
//
// JEV_LIVE_PROFILE=neutral asks with decision.prompt_profile=neutral (#2467 PR 6):
// the re-calibration the neutral rewrite needs before stage use. Run it next to the
// default run and compare p_yes per state with the threshold flag
// (docs/agent-group-ab-runbook.md, "Re-calibrating the jev gate").
//
//	JEV_API_KEY=... JEV_LIVE_PROFILE=neutral go test -tags live ./decide/backends/jev -run 'TestLiveGate$' -v
func TestLiveGate(t *testing.T) {
	key := liveKey(t)
	b := liveBackend(key, newRecordingTransport(key))
	profile := decide.NormalizePromptProfile(os.Getenv("JEV_LIVE_PROFILE"))
	t.Logf("prompt profile: %s", profile)
	for _, sc := range []struct {
		name string
		req  decide.Request
	}{
		{"calm", calmRequest()},
		{"dragging", draggingRequest()},
		{"near-threshold", testRequest()},
		{"collapsing", tooFastRequest()},
	} {
		sc.req.PromptProfile = profile
		plan := splitStages(sc.req)
		if plan.gated == nil {
			t.Fatalf("%s: request is not gated (ffa.intervention needs noop + an intervention)", sc.name)
		}
		start := time.Now()
		resp, err := b.call(context.Background(), plan.stage, plan.first, "")
		if err != nil {
			t.Fatalf("%s: gate call: %v", sc.name, err)
		}
		var pYes *float64
		pick := ""
		for _, a := range resp.Answers {
			switch a.QuestionID {
			case GateQuestionID:
				pYes = a.Noul
			case decide.QuestionFFAIntervention:
				pick = a.Choice
			}
		}
		if pYes == nil || *pYes < 0 || *pYes > 1 {
			t.Fatalf("%s: gate p_yes = %v, want a value in [0,1]", sc.name, pYes)
		}
		if pick == "" {
			t.Errorf("%s: no stage-1 intervention pick", sc.name)
		}
		t.Logf("%s: p_yes=%.2f pick=%s latency=%v", sc.name, *pYes, pick, time.Since(start))
	}
}

// TestLiveDetails records the details stage for the near-threshold state
// after a grant_shield and an adjust_player_sensitivity stage-1 answer, exactly
// as Decide conditions it, whatever the gate answers (one stage-1 call, then
// the conditioned details calls on the same connection pool). It logs whether
// Jev picks the player about to be eliminated and the level it picks. The
// grant_shield exchange is recorded as the shield_ fixtures, the
// adjust_player_sensitivity one as balance_. The act-round latency is measured
// by TestLiveDecide (the near-threshold state acts since the gate, #1281).
func TestLiveDetails(t *testing.T) {
	key := liveKey(t)
	rec := newRecordingTransport(key)
	b := liveBackend(key, rec)
	req := testRequest()
	start := time.Now()
	if _, err := b.call(context.Background(), StageIntervention, splitStages(req).first, ""); err != nil {
		t.Fatalf("stage 1: %v", err)
	}
	stage1 := time.Since(start)
	resp, _ := liveDetails(t, b, req, "grant_shield")
	ex := rec.last()
	t.Logf("act round (cold): stage1=%v details=%v total=%v", stage1, ex.elapsed, time.Since(start))
	logDetails(t, "grant_shield", resp)
	record(t, "shield_request_details.json", ex.request)
	record(t, "shield_response_details.json", ex.response)

	// Two-way lever (#1262 review I1): the direction comes from the chosen
	// target in Go, Jev answers the strength per side; log both sides and the
	// resolved magnitude.
	iv := "adjust_player_sensitivity"
	resp, raw := liveDetails(t, b, req, iv)
	for _, a := range raw.Answers {
		if dir := strengthDirection(a.QuestionID); dir != "" {
			v, _ := decide.ResolveMagnitude(iv, a.Score)
			t.Logf("  %s %s strength: %v (confidence %.2f)", iv, dir, v, a.Confidence)
		}
	}
	logDetails(t, iv, resp)
	ex = rec.last()
	record(t, "balance_request_details.json", ex.request)
	record(t, "balance_response_details.json", ex.response)
}

// liveDetails runs the details call for iv and returns the resolved answers
// (two-way strength picked by direction) and the raw details response.
func liveDetails(t *testing.T, b *Backend, req decide.Request, iv string) (decide.Response, decide.Response) {
	t.Helper()
	plan := detailsRequest(req, iv)
	raw, err := b.call(context.Background(), StageDetails, plan.ask, iv)
	if err != nil {
		t.Fatalf("%s details: %v", iv, err)
	}
	answers, err := plan.resolveTwoWay(req.State, append(append([]decide.Answer{}, raw.Answers...), plan.local...))
	if err != nil {
		t.Fatalf("%s resolve: %v", iv, err)
	}
	return decide.Response{Answers: answers}, raw
}

func logDetails(t *testing.T, iv string, resp decide.Response) {
	t.Helper()
	for _, a := range resp.Answers {
		t.Logf("  %s %s: choice=%q score=%.3f confidence=%.3f probabilities=%v", iv, a.QuestionID, a.Choice, a.Score, a.Confidence, topProbabilities(a.Probabilities, 4))
		if a.QuestionID == decide.QuestionFFAMagnitude {
			v, _ := decide.ResolveMagnitude(iv, a.Score)
			t.Logf("  %s magnitude resolves to %v", iv, v)
		}
	}
}

// TestLiveOptionOrder measures Jev 1.13's documented first-option bias on the
// gated stage-1 ffa.intervention question (#1262, #1281): the same stage-1
// request with the allow-list's first intervention first (production order)
// and moved last, JEV_ORDER_REPEATS times each (default 2), on the
// near-threshold and the calm state. It logs the moved option's probability,
// the choice and p_yes per run. Before the gate it measured noop first vs
// last (README).
func TestLiveOptionOrder(t *testing.T) {
	key := liveKey(t)
	repeats := 2
	if n, err := strconv.Atoi(os.Getenv("JEV_ORDER_REPEATS")); err == nil && n > 0 {
		repeats = n
	}
	b := liveBackend(key, newRecordingTransport(key))
	for _, sc := range []struct {
		name string
		req  decide.Request
	}{{"near-threshold", testRequest()}, {"calm", calmRequest()}} {
		first := splitStages(sc.req).first
		for _, order := range []string{"production", "first-last"} {
			req, moved := withFirstInterventionLast(first)
			if order == "production" {
				req = first
			}
			for r := 0; r < repeats; r++ {
				resp, err := b.call(context.Background(), StageIntervention, req, "")
				if err != nil {
					t.Fatalf("%s %s: %v", sc.name, order, err)
				}
				iv, _ := resp.Find(decide.QuestionFFAIntervention)
				gate, found := resp.Find(GateQuestionID)
				if !found || gate.Noul == nil {
					t.Fatalf("%s %s: no gate answer", sc.name, order)
				}
				t.Logf("%s %s #%d: p_yes=%.2f choice=%s p(%s)=%.2f top=%v", sc.name, order, r+1, *gate.Noul, iv.Choice, moved, iv.Probabilities[moved], topProbabilities(iv.Probabilities, 3))
			}
		}
	}
}

// withFirstInterventionLast returns the gated stage-1 req with
// ffa.intervention's first option moved last, and that option.
func withFirstInterventionLast(req decide.Request) (decide.Request, string) {
	out := decide.Request{State: req.State}
	moved := ""
	for _, q := range req.Questions {
		if q.ID == decide.QuestionFFAIntervention {
			moved = q.Options[0]
			opts := append([]string{}, q.Options[1:]...)
			q.Options = append(opts, moved)
		}
		out.Questions = append(out.Questions, q)
	}
	return out, moved
}

func resolvedMagnitude(resp decide.Response) (float64, bool) {
	iv, _ := resp.Find(decide.QuestionFFAIntervention)
	m, ok := resp.Find(decide.QuestionFFAMagnitude)
	if !ok {
		return 0, false
	}
	return decide.ResolveMagnitude(iv.Choice, m.Score)
}

func liveBackend(key string, rec *recordingTransport) *Backend {
	return New(Config{
		APIKey:     key,
		Model:      os.Getenv("JEV_LIVE_MODEL"),
		Timeout:    10 * time.Second,
		HTTPClient: &http.Client{Transport: rec, Timeout: 15 * time.Second},
	})
}

// TestLiveErrors records Jev's error envelopes: 401 (bad key, sent to the real
// endpoint) and 422 (a malformed question).
func TestLiveErrors(t *testing.T) {
	key := liveKey(t)
	cases := []struct {
		name, key, body, fixture string
		status                   int
	}{
		{"401", "not-a-real-key", `{"model":"jev-latest","state":"x","questions":{"q":{"type":"noul","instructions":"?"}}}`, "response_401.json", 401},
		{"422", key, `{"model":"jev-latest","state":"x","questions":{"q":{"type":"choice","instructions":"?"}}}`, "response_422.json", 422},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req, _ := http.NewRequest(http.MethodPost, DefaultBaseURL+wirePath, strings.NewReader(tc.body))
			setAuth(req.Header, tc.key)
			req.Header.Set("Content-Type", "application/json")
			resp, err := (&http.Client{Timeout: 15 * time.Second}).Do(req)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = resp.Body.Close() }()
			body, _ := io.ReadAll(resp.Body)
			if strings.Contains(string(body), key) {
				t.Fatal("error body echoes the API key")
			}
			t.Logf("%d: %s", resp.StatusCode, body)
			if resp.StatusCode != tc.status {
				t.Errorf("status = %d, want %d", resp.StatusCode, tc.status)
			}
			record(t, tc.fixture, body)
		})
	}
}

func liveKey(t *testing.T) string {
	t.Helper()
	key := strings.TrimSpace(os.Getenv("JEV_API_KEY"))
	if key == "" {
		t.Skip("JEV_API_KEY not set")
	}
	return key
}

type exchange struct {
	request, response []byte
	requestID         string
	elapsed           time.Duration
}

// recordingTransport keeps every request/response body and refuses to proceed
// if the key ever appears in a body. Each one owns its connection pool, so a
// new one starts cold.
type recordingTransport struct {
	key  string
	base http.RoundTripper
	mu   sync.Mutex
	ex   []exchange
}

func newRecordingTransport(key string) *recordingTransport {
	return &recordingTransport{key: key, base: http.DefaultTransport.(*http.Transport).Clone()}
}

func (r *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	reqBody, _ := io.ReadAll(req.Body)
	req.Body = io.NopCloser(bytes.NewReader(reqBody))
	start := time.Now()
	resp, err := r.base.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	respBody, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	elapsed := time.Since(start)
	resp.Body = io.NopCloser(bytes.NewReader(respBody))
	if bytes.Contains(reqBody, []byte(r.key)) || bytes.Contains(respBody, []byte(r.key)) {
		panic("API key found in a body")
	}
	r.mu.Lock()
	r.ex = append(r.ex, exchange{request: reqBody, response: respBody, requestID: resp.Header.Get(requestIDHeader), elapsed: elapsed})
	r.mu.Unlock()
	return resp, nil
}

func (r *recordingTransport) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.ex)
}

func (r *recordingTransport) since(n int) []exchange {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]exchange{}, r.ex[n:]...)
}

func (r *recordingTransport) last() exchange {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.ex[len(r.ex)-1]
}

// record writes body (indented) to testdata/name when JEV_RECORD_FIXTURES=1.
func record(t *testing.T, name string, body []byte) {
	t.Helper()
	if os.Getenv("JEV_RECORD_FIXTURES") != "1" {
		return
	}
	var out bytes.Buffer
	if err := json.Indent(&out, body, "", "  "); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
	out.WriteByte('\n')
	if err := os.WriteFile(filepath.Join("testdata", name), out.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}
}

func usageLine(body []byte) string {
	var r jevResponse
	_ = json.Unmarshal(body, &r)
	if r.Usage == nil || r.Usage.InputTokens == nil || r.Usage.OutputTokens == nil {
		return "usage=?"
	}
	return "response_model=" + r.Model + " input_tokens=" + itoa(*r.Usage.InputTokens) + " output_tokens=" + itoa(*r.Usage.OutputTokens)
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

// topProbabilities returns the k most probable options as "opt=p" strings.
func topProbabilities(p map[string]float64, k int) []string {
	type kv struct {
		k string
		v float64
	}
	var all []kv
	for o, v := range p {
		all = append(all, kv{o, v})
	}
	for i := range all {
		for j := i + 1; j < len(all); j++ {
			if all[j].v > all[i].v {
				all[i], all[j] = all[j], all[i]
			}
		}
	}
	var out []string
	for i := 0; i < len(all) && i < k; i++ {
		b, _ := json.Marshal(all[i].v)
		out = append(out, all[i].k+"="+string(b))
	}
	return out
}
