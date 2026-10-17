package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type daemonCall struct {
	method string
	path   string
}

// fakeDaemon answers like the pairing daemon and records each request.
func fakeDaemon(t *testing.T, status int, body string) (*httptest.Server, *[]daemonCall) {
	t.Helper()
	calls := &[]daemonCall{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*calls = append(*calls, daemonCall{r.Method, r.URL.EscapedPath()})
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv, calls
}

func postUnpair(t *testing.T, handler http.HandlerFunc, method, body string) (int, map[string]interface{}) {
	t.Helper()
	req := httptest.NewRequest(method, "/pairing/unpair", strings.NewReader(body))
	rec := httptest.NewRecorder()
	handler(rec, req)
	var decoded map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &decoded); err != nil {
		t.Fatalf("response is not JSON: %q", rec.Body.String())
	}
	return rec.Code, decoded
}

// UC: UC-005 main A4 A5 A7
func TestUnpairHandler(t *testing.T) {
	client := &http.Client{Timeout: 2 * time.Second}

	t.Run("UC-005/main forwards to the daemon and passes its answer through", func(t *testing.T) {
		daemon, calls := fakeDaemon(t, http.StatusOK, `{"address":"00:06:F7:AA:BB:CC","removed":2,"adapters":["AA:00:00:00:00:00","BB:00:00:00:00:00"]}`)
		status, body := postUnpair(t, unpairHandler(daemon.URL, client), http.MethodPost, `{"address":"00:06:F7:AA:BB:CC"}`)

		if status != http.StatusOK || body["removed"] != float64(2) {
			t.Fatalf("got %d %v", status, body)
		}
		want := []daemonCall{{http.MethodPost, "/controllers/00:06:F7:AA:BB:CC/unpair"}}
		if len(*calls) != 1 || (*calls)[0] != want[0] {
			t.Fatalf("daemon calls = %v, want %v", *calls, want)
		}
	})

	t.Run("UC-005/A4 daemon 404 is passed through", func(t *testing.T) {
		daemon, _ := fakeDaemon(t, http.StatusNotFound, `{"error":"Controller 00:06:F7:AA:BB:CC is not registered on any adapter."}`)
		status, body := postUnpair(t, unpairHandler(daemon.URL, client), http.MethodPost, `{"address":"00:06:F7:AA:BB:CC"}`)

		if status != http.StatusNotFound || !strings.Contains(body["error"].(string), "not registered") {
			t.Fatalf("got %d %v", status, body)
		}
	})

	t.Run("UC-005/A5 mock serial is rejected without calling the daemon", func(t *testing.T) {
		daemon, calls := fakeDaemon(t, http.StatusOK, `{}`)
		for _, payload := range []string{`{"address":"MOCK0001"}`, `{"address":"../../healthz"}`, `not json`, `{}`} {
			status, _ := postUnpair(t, unpairHandler(daemon.URL, client), http.MethodPost, payload)
			if status != http.StatusBadRequest {
				t.Fatalf("%s: got %d, want 400", payload, status)
			}
		}
		if len(*calls) != 0 {
			t.Fatalf("daemon was called: %v", *calls)
		}
	})

	t.Run("UC-005/A7 unreachable daemon is 502", func(t *testing.T) {
		daemon, _ := fakeDaemon(t, http.StatusOK, `{}`)
		daemon.Close()
		status, body := postUnpair(t, unpairHandler(daemon.URL, client), http.MethodPost, `{"address":"00:06:F7:AA:BB:CC"}`)

		if status != http.StatusBadGateway || !strings.Contains(body["error"].(string), "unreachable") {
			t.Fatalf("got %d %v", status, body)
		}
	})

	t.Run("only POST", func(t *testing.T) {
		status, _ := postUnpair(t, unpairHandler("http://127.0.0.1:1", client), http.MethodGet, "")
		if status != http.StatusMethodNotAllowed {
			t.Fatalf("got %d", status)
		}
	})
}
