package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
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

// UC: UC-005 main A3 A4 A5 A7
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

	t.Run("UC-005/A3 direct POST for an address with no card is forwarded as is", func(t *testing.T) {
		// No controller manager is wired in at all: the endpoint needs no card, only the address.
		daemon, calls := fakeDaemon(t, http.StatusOK, `{"address":"00:06:F7:11:22:33","removed":1,"on_usb":false}`)
		status, body := postUnpair(t, unpairHandler(daemon.URL, client), http.MethodPost, `{"address":"00:06:F7:11:22:33"}`)

		if status != http.StatusOK || body["removed"] != float64(1) {
			t.Fatalf("got %d %v", status, body)
		}
		want := daemonCall{http.MethodPost, "/controllers/00:06:F7:11:22:33/unpair"}
		if len(*calls) != 1 || (*calls)[0] != want {
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

// UC: UC-005 BR-003
func TestSameOriginOnly(t *testing.T) {
	called := 0
	h := sameOriginOnly([]string{"joustmania.local", "pi.local"}, func(w http.ResponseWriter, r *http.Request) {
		called++
		w.WriteHeader(http.StatusOK)
	})
	cases := []struct {
		name, host, origin, secFetchSite string
		want                             int
	}{
		{"UC-005/BR-003 no Origin (curl, scripts) passes", "joustmania.local", "", "", http.StatusOK},
		{"UC-005/BR-003 same-origin dashboard passes", "joustmania.local", "http://joustmania.local", "same-origin", http.StatusOK},
		{"UC-005/BR-003 foreign page is rejected", "joustmania.local", "https://evil.example", "", http.StatusForbidden},
		{"UC-005/BR-003 same host other port is rejected", "joustmania.local", "http://joustmania.local:8080", "", http.StatusForbidden},
		{"UC-005/BR-003 DNS-rebinding host is rejected", "evil.example", "http://evil.example", "same-origin", http.StatusForbidden},
		{"UC-005/BR-003 DNS-rebinding host without Origin is rejected", "evil.example", "", "", http.StatusForbidden},
		{"UC-005/BR-003 configured name with a port passes", "pi.local:80", "http://pi.local:80", "same-origin", http.StatusOK},
		{"UC-005/BR-003 configured name is case-insensitive", "JoustMania.Local", "", "", http.StatusOK},
		{"UC-005/BR-003 IPv4 literal passes", "192.168.1.20", "http://192.168.1.20", "same-origin", http.StatusOK},
		{"UC-005/BR-003 IPv4 literal with port passes", "10.0.0.5:80", "", "", http.StatusOK},
		{"UC-005/BR-003 IPv6 literal passes", "[::1]:80", "", "", http.StatusOK},
		{"UC-005/BR-003 bracketed IPv6 literal without port passes", "[::1]", "", "", http.StatusOK},
		{"UC-005/BR-003 localhost passes", "localhost", "http://localhost", "same-origin", http.StatusOK},
		{"UC-005/BR-003 localhost with port passes", "localhost:5173", "", "", http.StatusOK},
		{"UC-005/BR-003 name that merely contains an allowed name is rejected", "joustmania.local.evil.example", "", "", http.StatusForbidden},
		{"UC-005/BR-003 empty Host is rejected", "", "", "", http.StatusForbidden},
		{"UC-005/BR-003 Sec-Fetch-Site cross-site is rejected", "joustmania.local", "", "cross-site", http.StatusForbidden},
		{"UC-005/BR-003 Sec-Fetch-Site same-site is rejected", "joustmania.local", "", "same-site", http.StatusForbidden},
		{"UC-005/BR-003 Sec-Fetch-Site none (typed URL) passes", "joustmania.local", "", "none", http.StatusOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := called
			req := httptest.NewRequest(http.MethodPost, "http://joustmania.local/pairing/unpair", nil)
			req.Host = tc.host
			if tc.origin != "" {
				req.Header.Set("Origin", tc.origin)
			}
			if tc.secFetchSite != "" {
				req.Header.Set("Sec-Fetch-Site", tc.secFetchSite)
			}
			rec := httptest.NewRecorder()
			h(rec, req)
			if rec.Code != tc.want {
				t.Fatalf("got %d, want %d", rec.Code, tc.want)
			}
			if (tc.want == http.StatusOK) != (called == before+1) {
				t.Fatalf("handler called=%v, want %v", called != before, tc.want == http.StatusOK)
			}
		})
	}
}

// UC: UC-005 BR-003
func TestParseHostnames(t *testing.T) {
	t.Run("UC-005/BR-003 JOUSTMANIA_HOSTNAMES is split, trimmed and normalised", func(t *testing.T) {
		got := parseHostnames(" joustmania.local, Pi.Local. ,,")
		want := []string{"joustmania.local", "pi.local"}
		if strings.Join(got, "|") != strings.Join(want, "|") {
			t.Fatalf("got %v, want %v", got, want)
		}
	})
	t.Run("UC-005/BR-003 unset leaves only IPs and localhost", func(t *testing.T) {
		if got := parseHostnames(""); len(got) != 0 {
			t.Fatalf("got %v, want none", got)
		}
		if hostAllowed("joustmania.local", nil) {
			t.Fatal("a name is allowed without JOUSTMANIA_HOSTNAMES")
		}
	})
}

// UC: UC-005 BR-003
func TestHostHostnames(t *testing.T) {
	dir := t.TempDir()
	write := func(name, content string) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		return path
	}
	cases := []struct {
		name, path string
		want       []string
	}{
		{"UC-005/BR-003 host name file adds the name and its .local", write("plain", "raspberrypi"), []string{"raspberrypi", "raspberrypi.local"}},
		{"UC-005/BR-003 trailing newline and whitespace are trimmed", write("newline", "  RaspberryPi \n"), []string{"raspberrypi", "raspberrypi.local"}},
		{"UC-005/BR-003 a .local name is not doubled", write("mdns", "stage.local\n"), []string{"stage.local"}},
		{"UC-005/BR-003 empty file adds nothing", write("empty", " \n"), nil},
		{"UC-005/BR-003 absent file adds nothing", filepath.Join(dir, "missing"), nil},
		{"UC-005/BR-003 a directory (bind mount of a missing host file) adds nothing", dir, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := hostHostnames(tc.path)
			if strings.Join(got, "|") != strings.Join(tc.want, "|") {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
		})
	}

	t.Run("UC-005/BR-003 the host's mDNS name passes the allow-list", func(t *testing.T) {
		names := hostHostnames(write("pi", "raspberrypi\n"))
		if !hostAllowed("raspberrypi.local", names) || !hostAllowed("RaspberryPi:80", names) {
			t.Fatalf("host name not allowed with %v", names)
		}
		if hostAllowed("raspberrypi.local.evil.example", names) {
			t.Fatal("a suffix trick passes")
		}
	})
}
