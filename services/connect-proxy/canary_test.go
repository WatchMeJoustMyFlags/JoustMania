package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

// controllerFixture mirrors the shape of services/flagd/controller.json: the
// bluetooth_backend flag has a third "unstable" variant (agent rollout work),
// and the file holds other flags that the canary handlers must not touch.
const controllerFixture = `{
  "metadata": {"flagSetId": "controller", "version": "1"},
  "flags": {
    "backend": {
      "state": "ENABLED",
      "variants": {"mock": "mock", "python": "python"},
      "defaultVariant": "python"
    },
    "bluetooth_backend": {
      "state": "ENABLED",
      "variants": {"python": "python", "rust": "rust", "unstable": "unstable"},
      "defaultVariant": "python",
      "targeting": {}
    },
    "chaos_fault_type": {
      "state": "ENABLED",
      "variants": {"none": "none", "poll_drop": "poll_drop"},
      "defaultVariant": "none",
      "targeting": {}
    }
  }
}
`

// useFlagFile points the handlers at a temp copy of content and restores the
// package-level path afterwards. Tests using it must not run in parallel.
func useFlagFile(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "controller.json")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatalf("write fixture: %v", err)
	}
	previous := flagdConfigPath
	flagdConfigPath = path
	t.Cleanup(func() { flagdConfigPath = previous })
	return path
}

func newCanaryServer(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	registerCanaryHandlers(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func doRequest(t *testing.T, method, url string) (int, map[string]interface{}) {
	t.Helper()
	req, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatalf("new request: %v", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer resp.Body.Close()
	var body map[string]interface{}
	if resp.Header.Get("Content-Type") == "application/json" {
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode body: %v", err)
		}
	}
	return resp.StatusCode, body
}

// readFlags decodes the flag file generically so the assertions see exactly
// what flagd would read.
func readFlags(t *testing.T, path string) map[string]interface{} {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read flag file: %v", err)
	}
	var doc map[string]interface{}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("flag file is not valid JSON: %v", err)
	}
	return doc
}

func flagOf(t *testing.T, doc map[string]interface{}, key string) map[string]interface{} {
	t.Helper()
	flags, ok := doc["flags"].(map[string]interface{})
	if !ok {
		t.Fatalf("flag file has no flags object")
	}
	flag, ok := flags[key].(map[string]interface{})
	if !ok {
		t.Fatalf("flag %q missing", key)
	}
	return flag
}

// assertUntouched checks that everything except bluetooth_backend is
// semantically unchanged relative to the fixture.
func assertUntouched(t *testing.T, path string) {
	t.Helper()
	var want map[string]interface{}
	if err := json.Unmarshal([]byte(controllerFixture), &want); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	got := readFlags(t, path)
	if !reflect.DeepEqual(got["metadata"], want["metadata"]) {
		t.Errorf("metadata changed: got %v, want %v", got["metadata"], want["metadata"])
	}
	for _, key := range []string{"backend", "chaos_fault_type"} {
		if g, w := flagOf(t, got, key), flagOf(t, want, key); !reflect.DeepEqual(g, w) {
			t.Errorf("flag %q changed: got %v, want %v", key, g, w)
		}
	}
	if g, w := flagOf(t, got, "bluetooth_backend")["variants"], flagOf(t, want, "bluetooth_backend")["variants"]; !reflect.DeepEqual(g, w) {
		t.Errorf("bluetooth_backend variants changed: got %v, want %v", g, w)
	}
}

// fractionalShares returns the [variant, weight] buckets of a flagd
// fractional rule keyed on targetingKey, failing the test on any other shape.
func fractionalShares(t *testing.T, targeting interface{}) map[string]float64 {
	t.Helper()
	rule, ok := targeting.(map[string]interface{})
	if !ok {
		t.Fatalf("targeting is not an object: %#v", targeting)
	}
	args, ok := rule["fractional"].([]interface{})
	if !ok || len(args) < 2 {
		t.Fatalf("targeting is not a fractional rule: %#v", targeting)
	}
	bucketBy, ok := args[0].(map[string]interface{})
	if !ok || bucketBy["var"] != "targetingKey" {
		t.Errorf("fractional rule must bucket by targetingKey (the controller serial), got %#v", args[0])
	}
	shares := map[string]float64{}
	for _, raw := range args[1:] {
		bucket, ok := raw.([]interface{})
		if !ok || len(bucket) != 2 {
			t.Fatalf("bad fractional bucket %#v", raw)
		}
		shares[bucket[0].(string)] = bucket[1].(float64)
	}
	return shares
}

// UC: UC-052 main
func TestParseFraction(t *testing.T) {
	cases := []struct {
		name    string
		query   string
		want    int
		wantErr bool
	}{
		{"UC-052/main fraction defaults to 100", "", 100, false},
		{"UC-052/main fraction 1 is the lower bound", "fraction=1", 1, false},
		{"UC-052/main fraction 100 is the upper bound", "fraction=100", 100, false},
		{"UC-052/main fraction 25", "fraction=25", 25, false},
		{"UC-052/main fraction 0 rejected", "fraction=0", 0, true},
		{"UC-052/main fraction 101 rejected", "fraction=101", 0, true},
		{"UC-052/main negative fraction rejected", "fraction=-5", 0, true},
		{"UC-052/main non-numeric fraction rejected", "fraction=half", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/canary/rust?"+tc.query, nil)
			got, err := parseFraction(req)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected an error for %q, got %d", tc.query, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Errorf("parseFraction(%q) = %d, want %d", tc.query, got, tc.want)
			}
		})
	}
}

// UC: UC-052 main
func TestCanary_FullRolloutSetsDefaultAndClearsTargeting(t *testing.T) {
	for _, backend := range []string{"rust", "python"} {
		t.Run("UC-052/main canary "+backend+" at default fraction 100", func(t *testing.T) {
			path := useFlagFile(t, controllerFixture)
			srv := newCanaryServer(t)

			status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/"+backend)
			if status != http.StatusOK {
				t.Fatalf("status = %d, want 200", status)
			}
			if body["status"] != "ok" || body["backend"] != backend || body["fraction"] != float64(100) {
				t.Errorf("unexpected response body %v", body)
			}

			flag := flagOf(t, readFlags(t, path), "bluetooth_backend")
			if flag["defaultVariant"] != backend {
				t.Errorf("defaultVariant = %v, want %q", flag["defaultVariant"], backend)
			}
			if _, present := flag["targeting"]; present {
				t.Errorf("targeting must be cleared at fraction 100, got %#v", flag["targeting"])
			}
			assertUntouched(t, path)
		})
	}
}

// UC: UC-052 main
func TestCanary_FractionalWritesSerialTargeting(t *testing.T) {
	cases := []struct {
		backend     string
		fraction    string
		weight      float64
		counterpart string
	}{
		{"rust", "25", 25, "python"},
		{"python", "10", 10, "rust"},
	}
	for _, tc := range cases {
		t.Run("UC-052/main canary "+tc.backend+" at fraction "+tc.fraction, func(t *testing.T) {
			path := useFlagFile(t, controllerFixture)
			srv := newCanaryServer(t)

			status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/"+tc.backend+"?fraction="+tc.fraction)
			if status != http.StatusOK {
				t.Fatalf("status = %d, want 200", status)
			}
			if body["fraction"] != tc.weight {
				t.Errorf("response fraction = %v, want %v", body["fraction"], tc.weight)
			}

			flag := flagOf(t, readFlags(t, path), "bluetooth_backend")
			// The non-canary share defaults to the stable counterpart, never
			// to "unstable" (picked by name, not by map iteration order).
			if flag["defaultVariant"] != tc.counterpart {
				t.Errorf("defaultVariant = %v, want counterpart %q", flag["defaultVariant"], tc.counterpart)
			}
			shares := fractionalShares(t, flag["targeting"])
			if shares[tc.backend] != tc.weight {
				t.Errorf("canary share for %q = %v, want %v (all shares %v)", tc.backend, shares[tc.backend], tc.weight, shares)
			}
			total := 0.0
			for _, w := range shares {
				total += w
			}
			if total != 100 {
				t.Errorf("fractional weights sum to %v, want 100 (%v)", total, shares)
			}
			// Note (#1386): the remaining share is currently named "none",
			// which is not a bluetooth_backend variant, so flagd returns a
			// General error for those serials instead of the counterpart.
			// Deliberately not asserted here until #1386 is decided.
			assertUntouched(t, path)
		})
	}
}

// UC: UC-052 main
func TestCanary_CounterpartNeverUnstable(t *testing.T) {
	t.Run("UC-052/main repeated fractional canaries always fall back to the stable counterpart", func(t *testing.T) {
		path := useFlagFile(t, controllerFixture)
		// Go randomises map iteration order; repeat so an order-dependent
		// pick of "any other variant" would surface.
		for i := 0; i < 30; i++ {
			if err := setCanaryRouting("rust", 50); err != nil {
				t.Fatalf("setCanaryRouting: %v", err)
			}
			flag := flagOf(t, readFlags(t, path), "bluetooth_backend")
			if flag["defaultVariant"] != "python" {
				t.Fatalf("iteration %d: defaultVariant = %v, want python", i, flag["defaultVariant"])
			}
		}
	})
}

// UC: UC-052 main
func TestCanary_RollbackReturnsEveryoneToPython(t *testing.T) {
	t.Run("UC-052/main rollback after a fractional rust canary", func(t *testing.T) {
		path := useFlagFile(t, controllerFixture)
		srv := newCanaryServer(t)

		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=30"); status != http.StatusOK {
			t.Fatalf("canary status = %d, want 200", status)
		}
		status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/rollback")
		if status != http.StatusOK {
			t.Fatalf("rollback status = %d, want 200", status)
		}
		if body["backend"] != "python" {
			t.Errorf("rollback body backend = %v, want python", body["backend"])
		}

		flag := flagOf(t, readFlags(t, path), "bluetooth_backend")
		if flag["defaultVariant"] != "python" {
			t.Errorf("defaultVariant = %v, want python", flag["defaultVariant"])
		}
		if _, present := flag["targeting"]; present {
			t.Errorf("rollback must clear targeting, got %#v", flag["targeting"])
		}
		assertUntouched(t, path)
	})
}

// UC: UC-052 main
func TestCanary_StatusReturnsCurrentFlag(t *testing.T) {
	t.Run("UC-052/main status shows the canary just written", func(t *testing.T) {
		useFlagFile(t, controllerFixture)
		srv := newCanaryServer(t)

		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=40"); status != http.StatusOK {
			t.Fatalf("canary status = %d, want 200", status)
		}
		status, body := doRequest(t, http.MethodGet, srv.URL+"/canary/status")
		if status != http.StatusOK {
			t.Fatalf("status endpoint = %d, want 200", status)
		}
		if body["defaultVariant"] != "python" {
			t.Errorf("status defaultVariant = %v, want python", body["defaultVariant"])
		}
		if shares := fractionalShares(t, body["targeting"]); shares["rust"] != 40 {
			t.Errorf("status rust share = %v, want 40", shares["rust"])
		}
	})

	t.Run("UC-052/main status without bluetooth_backend is 404", func(t *testing.T) {
		useFlagFile(t, `{"flags": {}}`)
		srv := newCanaryServer(t)
		if status, _ := doRequest(t, http.MethodGet, srv.URL+"/canary/status"); status != http.StatusNotFound {
			t.Errorf("status = %d, want 404", status)
		}
	})
}

// The failure postcondition: whenever a canary call is rejected, the flag
// file is left byte-identical.
//
// UC: UC-052 main
func TestCanary_RejectedRequestLeavesFileUnchanged(t *testing.T) {
	cases := []struct {
		name   string
		method string
		path   string
		want   int
	}{
		{"UC-052/main fraction out of range is 400", http.MethodPost, "/canary/rust?fraction=0", http.StatusBadRequest},
		{"UC-052/main non-numeric fraction is 400", http.MethodPost, "/canary/rust?fraction=x", http.StatusBadRequest},
		{"UC-052/main GET on canary is 405", http.MethodGet, "/canary/rust", http.StatusMethodNotAllowed},
		{"UC-052/main GET on rollback is 405", http.MethodGet, "/canary/rollback", http.StatusMethodNotAllowed},
		{"UC-052/main POST on status is 405", http.MethodPost, "/canary/status", http.StatusMethodNotAllowed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			path := useFlagFile(t, controllerFixture)
			srv := newCanaryServer(t)

			status, _ := doRequest(t, tc.method, srv.URL+tc.path)
			if status != tc.want {
				t.Errorf("status = %d, want %d", status, tc.want)
			}
			data, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read flag file: %v", err)
			}
			if !bytes.Equal(data, []byte(controllerFixture)) {
				t.Errorf("flag file changed after a rejected request:\n%s", data)
			}
		})
	}
}

// UC: UC-052 main
func TestCanary_ServerErrors(t *testing.T) {
	t.Run("UC-052/main missing bluetooth_backend flag is 500 and file unchanged", func(t *testing.T) {
		const noBackend = `{"flags": {"backend": {"state": "ENABLED", "variants": {"python": "python"}, "defaultVariant": "python"}}}`
		path := useFlagFile(t, noBackend)
		srv := newCanaryServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust"); status != http.StatusInternalServerError {
			t.Errorf("status = %d, want 500", status)
		}
		if data, _ := os.ReadFile(path); string(data) != noBackend {
			t.Errorf("flag file changed: %s", data)
		}
	})

	t.Run("UC-052/main backend without a variant is 500", func(t *testing.T) {
		const content = `{"flags": {"bluetooth_backend": {"state": "ENABLED", "variants": {"python": "python"}, "defaultVariant": "python"}}}`
		path := useFlagFile(t, content)
		srv := newCanaryServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust"); status != http.StatusInternalServerError {
			t.Errorf("status = %d, want 500", status)
		}
		if data, _ := os.ReadFile(path); !bytes.Equal(data, []byte(content)) {
			t.Errorf("flag file changed: %s", data)
		}
	})

	t.Run("UC-052/main fractional canary without a stable counterpart is 500", func(t *testing.T) {
		const content = `{"flags": {"bluetooth_backend": {"state": "ENABLED", "variants": {"rust": "rust", "unstable": "unstable"}, "defaultVariant": "rust"}}}`
		path := useFlagFile(t, content)
		srv := newCanaryServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=50"); status != http.StatusInternalServerError {
			t.Errorf("status = %d, want 500", status)
		}
		if data, _ := os.ReadFile(path); !bytes.Equal(data, []byte(content)) {
			t.Errorf("flag file changed: %s", data)
		}
	})

	t.Run("UC-052/main unreadable flag file is 500", func(t *testing.T) {
		previous := flagdConfigPath
		flagdConfigPath = filepath.Join(t.TempDir(), "missing.json")
		t.Cleanup(func() { flagdConfigPath = previous })
		srv := newCanaryServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust"); status != http.StatusInternalServerError {
			t.Errorf("set status = %d, want 500", status)
		}
		if status, _ := doRequest(t, http.MethodGet, srv.URL+"/canary/status"); status != http.StatusInternalServerError {
			t.Errorf("status endpoint = %d, want 500", status)
		}
	})
}

// flagd watches the single bind-mounted controller.json by stat polling;
// replacing it via temp file + rename would detach the bind mount. The
// handlers must therefore rewrite the same file in place.
//
// UC: UC-052 main
func TestCanary_WritesFlagFileInPlace(t *testing.T) {
	t.Run("UC-052/main canary keeps the same file (inode) and valid JSON", func(t *testing.T) {
		path := useFlagFile(t, controllerFixture)
		before, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if err := setCanaryRouting("rust", 20); err != nil {
			t.Fatalf("setCanaryRouting: %v", err)
		}
		after, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if !os.SameFile(before, after) {
			t.Errorf("flag file was replaced, not rewritten in place")
		}
		data, _ := os.ReadFile(path)
		if len(data) == 0 || data[len(data)-1] != '\n' {
			t.Errorf("flag file must end with a newline")
		}
		readFlags(t, path)
	})
}
