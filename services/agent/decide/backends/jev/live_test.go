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
// JEV_LIVE_MODEL overrides the model (default DefaultModel). With
// JEV_RECORD_FIXTURES=1 the request/response bodies are written to testdata/
// (the API key travels only in the Authorization header, never in a body, and
// recordingTransport asserts that).
func TestLiveDecide(t *testing.T) {
	key := liveKey(t)
	rec := &recordingTransport{key: key}
	b := New(Config{
		APIKey:     key,
		Model:      os.Getenv("JEV_LIVE_MODEL"),
		Timeout:    10 * time.Second,
		HTTPClient: &http.Client{Transport: rec, Timeout: 15 * time.Second},
	})
	req := testRequest()
	// Two identical calls: the first pays DNS + TLS setup (cold), the second is
	// the steady-state in-game latency (warm, pooled connection).
	for i, label := range []string{"cold", "warm"} {
		start := time.Now()
		resp, err := b.Decide(context.Background(), req)
		elapsed := time.Since(start)
		if err != nil {
			t.Fatalf("%s call: Decide: %v (after %v)", label, err, elapsed)
		}
		ex := rec.last()
		t.Logf("%s: model=%s latency=%v request_id=%s %s", label, b.Model(), elapsed, ex.requestID, usageLine(ex.response))
		for _, a := range resp.Answers {
			t.Logf("  %s: choice=%q score=%.3f confidence=%.3f probabilities=%v", a.QuestionID, a.Choice, a.Score, a.Confidence, topProbabilities(a.Probabilities, 4))
		}
		t.Logf("  native confidence: %s", nativeLine(t, req, ex.response))
		if i == 0 {
			record(t, "request_ffa.json", ex.request)
			record(t, "response_ok.json", ex.response)
		}
		if i == 1 && elapsed > DefaultTimeout {
			t.Errorf("steady-state latency %v exceeds the %v in-game budget", elapsed, DefaultTimeout)
		}
	}

	// A noul request (#1229 KindNoul).
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
}

// recordingTransport keeps the last request/response bodies and refuses to
// proceed if the key ever appears in a body.
type recordingTransport struct {
	key string
	mu  sync.Mutex
	ex  []exchange
}

func (r *recordingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	reqBody, _ := io.ReadAll(req.Body)
	req.Body = io.NopCloser(bytes.NewReader(reqBody))
	resp, err := http.DefaultTransport.RoundTrip(req)
	if err != nil {
		return nil, err
	}
	respBody, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	resp.Body = io.NopCloser(bytes.NewReader(respBody))
	if bytes.Contains(reqBody, []byte(r.key)) || bytes.Contains(respBody, []byte(r.key)) {
		panic("API key found in a body")
	}
	r.mu.Lock()
	r.ex = append(r.ex, exchange{request: reqBody, response: respBody, requestID: resp.Header.Get(requestIDHeader)})
	r.mu.Unlock()
	return resp, nil
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

func nativeLine(t *testing.T, req decide.Request, body []byte) string {
	res, _, err := decodeResponse(req, body)
	if err != nil {
		t.Errorf("decode: %v", err)
	}
	return nativeConfidenceJSON(res.NativeConfidence)
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
