package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeFlags is an in-memory flag service for the controller set: declared
// variants per flag, a stack overlay and a fraction overlay, and a call log.
type fakeFlags struct {
	mu       sync.Mutex
	variants map[string][]string // flag -> declared variant names (value == name)
	baseline map[string]string
	stack    map[string]string
	fraction map[string][2]any // flag -> {variant, percent}
	calls    []string
	failGet  error
	failPut  error
}

func newFakeFlags() *fakeFlags {
	return &fakeFlags{
		variants: map[string][]string{
			"bluetooth_backend": {"python", "rust", "unstable"},
			"chaos_fault_type":  {"none", "poll_drop", "accel_spike", "led_failure", "disconnect"},
		},
		baseline: map[string]string{"bluetooth_backend": "rust", "chaos_fault_type": "none"},
		stack:    map[string]string{},
		fraction: map[string][2]any{},
	}
}

func (f *fakeFlags) PutVariant(_ context.Context, flag, variant string, fraction int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, "PUT "+flag+" "+variant+" "+itoa(fraction))
	if f.failPut != nil {
		return f.failPut
	}
	declared := false
	for _, v := range f.variants[flag] {
		declared = declared || v == variant
	}
	if !declared {
		return &flagServiceError{Status: 422, Reason: "out_of_bounds"}
	}
	if fraction > 0 {
		f.fraction[flag] = [2]any{variant, fraction}
	} else {
		f.stack[flag] = variant
	}
	return nil
}

func (f *fakeFlags) Clear(_ context.Context, flag string, fraction bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if fraction {
		f.calls = append(f.calls, "DELETE "+flag+" fraction")
		delete(f.fraction, flag)
	} else {
		f.calls = append(f.calls, "DELETE "+flag+" stack")
		delete(f.stack, flag)
	}
	return nil
}

func (f *fakeFlags) Get(_ context.Context, flag string) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failGet != nil {
		return nil, f.failGet
	}
	names, ok := f.variants[flag]
	if !ok {
		return nil, &flagServiceError{Status: 404, Reason: "unknown_flag"}
	}
	values := map[string]any{}
	list := make([]any, 0, len(names))
	for _, n := range names {
		values[n] = n
		list = append(list, n)
	}
	view := map[string]any{"flag": flag, "variants": list, "variant_values": values, "effective_default": f.effective(flag)}
	if fr, ok := f.fraction[flag]; ok {
		view["fraction_overlay"] = map[string]any{"variant": fr[0], "fraction_percent": fr[1]}
	}
	return view, nil
}

func (f *fakeFlags) effective(flag string) string {
	if v, ok := f.stack[flag]; ok {
		return v
	}
	return f.baseline[flag]
}

func itoa(n int) string {
	b, _ := json.Marshal(n)
	return string(b)
}

// useFakeFlags installs a fresh fake for the test.
func useFakeFlags(t *testing.T) *fakeFlags {
	t.Helper()
	fake := newFakeFlags()
	previous := flags
	flags = fake
	t.Cleanup(func() { flags = previous })
	return fake
}

func newProxyServer(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	registerCanaryHandlers(mux)
	registerChaosHandlers(mux)
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
// UC: UC-053 A1
func TestCanary_FullRolloutIsAStackOverlay(t *testing.T) {
	for _, backend := range []string{"rust", "python"} {
		t.Run("UC-052/main canary "+backend+" at default fraction 100", func(t *testing.T) {
			fake := useFakeFlags(t)
			fake.fraction["bluetooth_backend"] = [2]any{"rust", 30}
			srv := newProxyServer(t)

			status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/"+backend)
			if status != http.StatusOK {
				t.Fatalf("status = %d, want 200", status)
			}
			if body["status"] != "ok" || body["backend"] != backend || body["fraction"] != float64(100) {
				t.Errorf("unexpected response body %v", body)
			}
			if got := fake.effective("bluetooth_backend"); got != backend {
				t.Errorf("effective default = %s, want %s", got, backend)
			}
			if _, present := fake.fraction["bluetooth_backend"]; present {
				t.Error("a full rollout must clear the fraction overlay")
			}
		})
	}
}

// UC: UC-052 main
func TestCanary_FractionalIsAFractionOverlayOverTheCounterpart(t *testing.T) {
	cases := []struct {
		backend, counterpart string
		fraction             int
	}{
		{"rust", "python", 25},
		{"python", "rust", 10},
	}
	for _, tc := range cases {
		t.Run("UC-052/main canary "+tc.backend+" at fraction "+itoa(tc.fraction), func(t *testing.T) {
			fake := useFakeFlags(t)
			srv := newProxyServer(t)

			status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/"+tc.backend+"?fraction="+itoa(tc.fraction))
			if status != http.StatusOK {
				t.Fatalf("status = %d, want 200", status)
			}
			if body["fraction"] != float64(tc.fraction) {
				t.Errorf("response fraction = %v", body["fraction"])
			}
			// The rest of the serials resolve the stable counterpart (#1386), never "unstable".
			if got := fake.effective("bluetooth_backend"); got != tc.counterpart {
				t.Errorf("default = %s, want counterpart %s", got, tc.counterpart)
			}
			if got := fake.fraction["bluetooth_backend"]; got != [2]any{tc.backend, tc.fraction} {
				t.Errorf("fraction overlay = %v", got)
			}
		})
	}
}

// UC: UC-052 main
func TestCanary_RollbackReturnsEveryoneToPython(t *testing.T) {
	t.Run("UC-052/main rollback after a fractional rust canary", func(t *testing.T) {
		fake := useFakeFlags(t)
		srv := newProxyServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=30"); status != http.StatusOK {
			t.Fatalf("canary status = %d", status)
		}
		status, body := doRequest(t, http.MethodPost, srv.URL+"/canary/rollback")
		if status != http.StatusOK || body["backend"] != "python" {
			t.Fatalf("rollback = %d %v", status, body)
		}
		if got := fake.effective("bluetooth_backend"); got != "python" {
			t.Errorf("default = %s, want python", got)
		}
		if _, present := fake.fraction["bluetooth_backend"]; present {
			t.Error("rollback must clear the fraction overlay")
		}
	})
}

// UC: UC-052 main
func TestCanary_StatusIsTheFlagServiceView(t *testing.T) {
	fake := useFakeFlags(t)
	srv := newProxyServer(t)
	if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=40"); status != http.StatusOK {
		t.Fatalf("canary status = %d", status)
	}
	status, body := doRequest(t, http.MethodGet, srv.URL+"/canary/status")
	if status != http.StatusOK || body["effective_default"] != "python" {
		t.Fatalf("status = %d %v", status, body)
	}
	if fo, _ := body["fraction_overlay"].(map[string]any); fo["variant"] != "rust" {
		t.Errorf("fraction overlay %v", body["fraction_overlay"])
	}
	fake.failGet = &flagServiceError{Detail: "connection refused"}
	if status, _ := doRequest(t, http.MethodGet, srv.URL+"/canary/status"); status != http.StatusBadGateway {
		t.Errorf("status with the flag service down = %d, want 502", status)
	}
}

// A rejected request writes nothing.
//
// UC: UC-052 main
func TestCanary_RejectedRequestWritesNothing(t *testing.T) {
	cases := []struct {
		name, method, path string
		want               int
	}{
		{"UC-052/main fraction out of range is 400", http.MethodPost, "/canary/rust?fraction=0", http.StatusBadRequest},
		{"UC-052/main non-numeric fraction is 400", http.MethodPost, "/canary/rust?fraction=x", http.StatusBadRequest},
		{"UC-052/main GET on canary is 405", http.MethodGet, "/canary/rust", http.StatusMethodNotAllowed},
		{"UC-052/main GET on rollback is 405", http.MethodGet, "/canary/rollback", http.StatusMethodNotAllowed},
		{"UC-052/main POST on status is 405", http.MethodPost, "/canary/status", http.StatusMethodNotAllowed},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := useFakeFlags(t)
			srv := newProxyServer(t)
			if status, _ := doRequest(t, tc.method, srv.URL+tc.path); status != tc.want {
				t.Errorf("status = %d, want %d", status, tc.want)
			}
			for _, c := range fake.calls {
				if !strings.HasPrefix(c, "GET") {
					t.Errorf("write after a rejected request: %v", fake.calls)
				}
			}
		})
	}
}

// UC: UC-052 main
// UC: UC-053 A8
func TestCanary_FlagServiceErrors(t *testing.T) {
	t.Run("UC-053/A8 flag service unreachable is 502", func(t *testing.T) {
		fake := useFakeFlags(t)
		fake.failGet = &flagServiceError{Detail: "connection refused"}
		srv := newProxyServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust"); status != http.StatusBadGateway {
			t.Errorf("status = %d, want 502", status)
		}
	})
	t.Run("UC-053/A7 no sync stream is 503", func(t *testing.T) {
		fake := useFakeFlags(t)
		fake.failPut = &flagServiceError{Status: 503, Reason: "no_sync_stream"}
		srv := newProxyServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust"); status != http.StatusServiceUnavailable {
			t.Errorf("status = %d, want 503", status)
		}
	})
	t.Run("UC-052/main fractional canary without a stable counterpart is 500", func(t *testing.T) {
		fake := useFakeFlags(t)
		fake.variants["bluetooth_backend"] = []string{"rust", "unstable"}
		srv := newProxyServer(t)
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/canary/rust?fraction=50"); status != http.StatusInternalServerError {
			t.Errorf("status = %d, want 500", status)
		}
		if len(fake.stack)+len(fake.fraction) != 0 {
			t.Error("nothing may be written")
		}
	})
}

// UC: UC-052 main
func TestChaos(t *testing.T) {
	t.Run("UC-052/main a fault for every controller is a stack overlay", func(t *testing.T) {
		fake := useFakeFlags(t)
		srv := newProxyServer(t)
		if status, body := doRequest(t, http.MethodPost, srv.URL+"/chaos/poll-drop"); status != http.StatusOK || body["fault"] != "poll_drop" {
			t.Fatalf("chaos = %d %v", status, body)
		}
		if got := fake.effective("chaos_fault_type"); got != "poll_drop" {
			t.Errorf("default = %s", got)
		}
	})
	t.Run("UC-052/main a fractional fault keeps the default none", func(t *testing.T) {
		fake := useFakeFlags(t)
		srv := newProxyServer(t)
		doRequest(t, http.MethodPost, srv.URL+"/chaos/led-failure")
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/chaos/disconnect?fraction=20"); status != http.StatusOK {
			t.Fatalf("status %d", status)
		}
		if got := fake.effective("chaos_fault_type"); got != "none" {
			t.Errorf("default = %s, want none", got)
		}
		if got := fake.fraction["chaos_fault_type"]; got != [2]any{"disconnect", 20} {
			t.Errorf("fraction overlay %v", got)
		}
	})
	t.Run("UC-052/main reset clears both overlays", func(t *testing.T) {
		fake := useFakeFlags(t)
		srv := newProxyServer(t)
		doRequest(t, http.MethodPost, srv.URL+"/chaos/accel-spike?fraction=50")
		if status, _ := doRequest(t, http.MethodPost, srv.URL+"/chaos/reset"); status != http.StatusOK {
			t.Fatalf("reset %d", status)
		}
		if len(fake.stack)+len(fake.fraction) != 0 {
			t.Errorf("overlays left: %v %v", fake.stack, fake.fraction)
		}
	})
	t.Run("UC-052/main status is the flag service view", func(t *testing.T) {
		useFakeFlags(t)
		srv := newProxyServer(t)
		if status, body := doRequest(t, http.MethodGet, srv.URL+"/chaos/status"); status != http.StatusOK || body["effective_default"] != "none" {
			t.Fatalf("status %d %v", status, body)
		}
	})
}

// The HTTP client sends the requests the flag service API defines
// (docs/specs/services.md §7.2) and maps its errors.
//
// UC: UC-053 A1
func TestHTTPFlagService(t *testing.T) {
	var got []string
	var bodies []map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.Method+" "+r.URL.RequestURI())
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		bodies = append(bodies, body)
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.Method == http.MethodPut && body["variant"] == "nope":
			w.WriteHeader(422)
			_, _ = w.Write([]byte(`{"error":"out_of_bounds","detail":"not declared"}`))
		case r.Method == http.MethodDelete:
			w.WriteHeader(204)
		default:
			_, _ = w.Write([]byte(`{"flag":"bluetooth_backend"}`))
		}
	}))
	defer srv.Close()
	s := &httpFlagService{base: srv.URL, client: srv.Client()}
	ctx := context.Background()

	if err := s.PutVariant(ctx, "bluetooth_backend", "rust", 0); err != nil {
		t.Fatal(err)
	}
	if err := s.PutVariant(ctx, "bluetooth_backend", "rust", 25); err != nil {
		t.Fatal(err)
	}
	if err := s.Clear(ctx, "bluetooth_backend", true); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Get(ctx, "bluetooth_backend"); err != nil {
		t.Fatal(err)
	}
	want := []string{
		"PUT /controller/bluetooth_backend",
		"PUT /controller/bluetooth_backend",
		"DELETE /controller/bluetooth_backend?writer=connect-proxy&fraction=true",
		"GET /controller/bluetooth_backend",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("requests:\n%s\nwant:\n%s", strings.Join(got, "\n"), strings.Join(want, "\n"))
	}
	if bodies[0]["writer"] != proxyWriter || bodies[0]["scope"] != nil {
		t.Errorf("stack write body %v", bodies[0])
	}
	if scope, _ := bodies[1]["scope"].(map[string]any); scope["fraction_percent"] != float64(25) {
		t.Errorf("fraction write body %v", bodies[1])
	}
	err := s.PutVariant(ctx, "bluetooth_backend", "nope", 0)
	if fe, ok := err.(*flagServiceError); !ok || fe.Status != 422 || fe.Reason != "out_of_bounds" || statusFor(err) != 422 {
		t.Fatalf("refusal = %v", err)
	}
	down := &httpFlagService{base: "http://127.0.0.1:1", client: srv.Client()}
	if err := down.Clear(ctx, "x", false); statusFor(err) != http.StatusBadGateway {
		t.Fatalf("outage = %v", err)
	}
}
