package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/syncserver"
	"github.com/joustmania/flag-service/telemetry"
)

type env struct {
	t      *testing.T
	srv    *httptest.Server
	st     *state.State
	spans  *tracetest.SpanRecorder
	reader *sdkmetric.ManualReader
}

func newEnv(t *testing.T, stream bool) *env {
	t.Helper()
	return newEnvRegistry(t, stream, "")
}

// newEnvRegistry is newEnv with the registry document regYAML in place of
// registry.yaml when it is not empty.
func newEnvRegistry(t *testing.T, stream bool, regYAML string) *env {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Join(filepath.Dir(file), "..")
	base, err := baseline.Load(filepath.Join(root, "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	var reg *registry.Registry
	if regYAML != "" {
		reg, err = registry.Parse([]byte(regYAML), base)
	} else {
		reg, err = registry.LoadFile(filepath.Join(root, "registry.yaml"), base)
	}
	if err != nil {
		t.Fatal(err)
	}
	st, _, err := state.New(base, reg, state.Options{Epoch: "e1", Served: base.Order, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		t.Fatal(err)
	}
	if stream {
		sub, _ := st.Subscribe("")
		t.Cleanup(sub.Close)
	}
	spans := tracetest.NewSpanRecorder()
	reader := sdkmetric.NewManualReader()
	metrics, err := telemetry.NewMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter("t"), st.Stats)
	if err != nil {
		t.Fatal(err)
	}
	a := &API{State: st, Metrics: metrics, Tracer: sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans)).Tracer("t"), Links: &syncserver.Links{}}
	srv := httptest.NewServer(a.Handler())
	t.Cleanup(srv.Close)
	return &env{t: t, srv: srv, st: st, spans: spans, reader: reader}
}

func (e *env) do(method, path string, body any) (int, map[string]any) {
	e.t.Helper()
	var rd io.Reader
	if body != nil {
		if s, ok := body.(string); ok {
			rd = strings.NewReader(s)
		} else {
			raw, _ := json.Marshal(body)
			rd = bytes.NewReader(raw)
		}
	}
	req, _ := http.NewRequest(method, e.srv.URL+path, rd)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	out := map[string]any{}
	raw, _ := io.ReadAll(resp.Body)
	_ = json.Unmarshal(raw, &out)
	return resp.StatusCode, out
}

func (e *env) writeSpan(flag string) map[attribute.Key]attribute.Value {
	e.t.Helper()
	for i := len(e.spans.Ended()) - 1; i >= 0; i-- {
		s := e.spans.Ended()[i]
		if s.Name() != telemetry.SpanWrite {
			continue
		}
		attrs := map[attribute.Key]attribute.Value{}
		for _, kv := range s.Attributes() {
			attrs[kv.Key] = kv.Value
		}
		if attrs["feature_flag.key"].AsString() == flag {
			return attrs
		}
	}
	e.t.Fatalf("no %s span for %s", telemetry.SpanWrite, flag)
	return nil
}

func (e *env) writes(result, reason string) int64 {
	e.t.Helper()
	var rm metricdata.ResourceMetrics
	_ = e.reader.Collect(context.Background(), &rm)
	var n int64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != "flagservice_writes_total" {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				r, _ := dp.Attributes.Value("result")
				why, _ := dp.Attributes.Value("reason")
				if r.AsString() == result && why.AsString() == reason {
					n += dp.Value
				}
			}
		}
	}
	return n
}

// UC: UC-053 main A5 BR-005 Post-S-2 Post-F-1
func TestWriteAudit(t *testing.T) {
	t.Run("UC-053/main an accepted write returns a WriteResult, a span and a counted ok", func(t *testing.T) {
		e := newEnv(t, true)
		e.do("PUT", "/games/game_a", map[string]any{"coordinator_instance": "c/1", "game_kind": "shadow", "serials": []string{"MOCK0001"}})
		status, body := e.do("PUT", "/interventions/shield_seconds", map[string]any{
			"value": 5, "scope": map[string]any{"game_id": "game_a", "serial": "MOCK0001"},
			"writer": "agent", "decision_id": "dec_7", "reason": "grant_shield",
		})
		if status != 200 || body["epoch"] != "e1" || body["snapshot_version"] == nil || !strings.HasPrefix(body["variant"].(string), "re1_") {
			t.Fatalf("%d %v", status, body)
		}
		attrs := e.writeSpan("shield_seconds")
		want := map[attribute.Key]string{
			"feature_flag.set.id": "interventions", "flagservice.op": "set", "flagservice.kind": "grant",
			"flagservice.scope": "game+serial", "flagservice.result": "ok", "flagservice.writer": "agent",
			"game.id": "game_a", "game.kind": "shadow", "player.serial": "MOCK0001", "decision.intervention_id": "dec_7",
			"feature_flag.result.value": "5",
		}
		for k, v := range want {
			if attrs[k].AsString() != v {
				t.Errorf("%s = %q, want %q", k, attrs[k].AsString(), v)
			}
		}
		if e.writes("ok", "ok") != 1 {
			t.Fatal("flagservice_writes_total{result=ok} not counted")
		}
	})
	t.Run("UC-053/A5 a rejected write is a span and a counted rejection with its reason", func(t *testing.T) {
		e := newEnv(t, true)
		version := e.st.Version()
		status, body := e.do("PUT", "/system/idle.enabled", map[string]any{"variant": "off", "writer": "stage"})
		if status != http.StatusUnprocessableEntity || body["error"] != "flag_read_only" || body["detail"] == "" {
			t.Fatalf("%d %v", status, body)
		}
		if e.st.Version() != version {
			t.Fatal("a rejected write changed the configuration flagd serves (Post-F-1)")
		}
		attrs := e.writeSpan("idle.enabled")
		if attrs["flagservice.result"].AsString() != "rejected" || attrs["flagservice.reason"].AsString() != "flag_read_only" {
			t.Fatalf("span %v", attrs)
		}
		if e.writes("rejected", "flag_read_only") != 1 {
			t.Fatal("rejection not counted")
		}
	})
}

// UC: UC-053 A5 A6 A7
func TestHTTPStatuses(t *testing.T) {
	e := newEnv(t, true)
	e.do("PUT", "/games/game_a", map[string]any{"coordinator_instance": "c/1", "serials": []string{"S1"}})
	cases := []struct {
		name, method, path string
		body               any
		status             int
		reason             string
	}{
		{"UC-053/A5 bad JSON", "PUT", "/agent/enabled", "{", 400, "malformed"},
		{"UC-053/A5 unknown body field", "PUT", "/agent/enabled", map[string]any{"variant": "on", "owner": "x"}, 400, "malformed"},
		{"UC-053/A5 empty body", "PUT", "/agent/enabled", "", 400, "malformed"},
		{"UC-053/A5 unknown set", "PUT", "/nope/x", map[string]any{"variant": "a"}, 404, "unknown_flag_set"},
		{"UC-053/A5 unknown flag", "GET", "/agent/nope", nil, 404, "unknown_flag"},
		{"UC-053/A6 game not live", "POST", "/interventions/end_game", map[string]any{"scope": map[string]any{"game_id": "game_zz"}}, 409, "game_not_live"},
		{"UC-053/A5 serial not in game", "POST", "/interventions/eliminate_player", map[string]any{"scope": map[string]any{"game_id": "game_a"}, "payload": map[string]any{"serial": "S9"}}, 422, "serial_not_in_game"},
		{"UC-053/A5 DELETE needs a writer for a set reset", "DELETE", "/agent", nil, 400, "malformed"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			status, body := e.do(c.method, c.path, c.body)
			if status != c.status || body["error"] != c.reason {
				t.Fatalf("%d %v, want %d %s", status, body, c.status, c.reason)
			}
		})
	}
	t.Run("UC-053/A7 no stream: flag writes 503, the lifecycle is exempt", func(t *testing.T) {
		e := newEnv(t, false)
		if s, b := e.do("PUT", "/agent/enabled", map[string]any{"variant": "on"}); s != 503 || b["error"] != "no_sync_stream" {
			t.Fatalf("%d %v", s, b)
		}
		if s, _ := e.do("PUT", "/games/g1", map[string]any{"coordinator_instance": "c/1"}); s != 200 {
			t.Fatalf("register without a stream: %d", s)
		}
		if s, _ := e.do("PUT", "/coordinators/c%2F1/games", map[string]any{"games": []any{}}); s != 200 {
			t.Fatalf("reconcile without a stream: %d", s)
		}
		if s, _ := e.do("DELETE", "/games/g1", nil); s != 204 {
			t.Fatalf("end without a stream: %d", s)
		}
	})
}

// UC: UC-053 main A4 A10 BR-003
func TestLifecycleAndEvents(t *testing.T) {
	e := newEnv(t, true)
	instance := "coord-host/3f2a"
	path := "/coordinators/" + url.PathEscape(instance) + "/games"
	t.Run("UC-053/A10 the reconcile path carries an instance id with a slash", func(t *testing.T) {
		status, body := e.do("PUT", path, map[string]any{"games": []any{
			map[string]any{"game_id": "game_1", "game_kind": "real", "serials": []string{"S1", "S2"}},
		}})
		if status != 200 || body["coordinator_instance"] != instance {
			t.Fatalf("%d %v", status, body)
		}
		g := e.st.Game("game_1")
		if g == nil || g.CoordinatorInstance != instance {
			t.Fatalf("game %+v", g)
		}
	})
	t.Run("UC-053/A4 POST appends and returns the seq", func(t *testing.T) {
		for want := 1.0; want <= 2; want++ {
			status, body := e.do("POST", "/interventions/eliminate_player", map[string]any{
				"scope": map[string]any{"game_id": "game_1"}, "payload": map[string]any{"serial": "S1"},
			})
			if status != 200 || body["seq"] != want {
				t.Fatalf("%d %v", status, body)
			}
		}
		if attrs := e.writeSpan("eliminate_player"); attrs["flagservice.op"].AsString() != "append" || attrs["flagservice.seq"].AsInt64() != 2 {
			t.Fatalf("span %v", attrs)
		}
	})
	t.Run("UC-053/main GET shows the event list; DELETE /games drops it", func(t *testing.T) {
		status, body := e.do("GET", "/interventions/eliminate_player", nil)
		if status != 200 || body["events"] == nil || body["rendered"] == nil {
			t.Fatalf("%d %v", status, body)
		}
		if s, _ := e.do("DELETE", "/games/game_1", nil); s != 204 {
			t.Fatal(s)
		}
		_, body = e.do("GET", "/interventions/eliminate_player", nil)
		if body["events"] != nil {
			t.Fatalf("events left after the game ended: %v", body["events"])
		}
		_, games := e.do("GET", "/games", nil)
		if len(games["games"].([]any)) != 0 {
			t.Fatalf("games %v", games)
		}
	})
	t.Run("UC-053/main a repeat registration says already_registered; a body id must match the path", func(t *testing.T) {
		e.do("PUT", "/games/game_2", map[string]any{"coordinator_instance": "c/1"})
		if _, b := e.do("PUT", "/games/game_2", map[string]any{"coordinator_instance": "c/1"}); b["status"] != "already_registered" {
			t.Fatalf("%v", b)
		}
		if s, _ := e.do("PUT", "/games/game_2", map[string]any{"coordinator_instance": "c/1", "game_id": "other"}); s != 400 {
			t.Fatalf("mismatched id: %d", s)
		}
	})
}

// UC: UC-053 main
func TestReadEndpoints(t *testing.T) {
	e := newEnv(t, true)
	t.Run("UC-053/main GET /, /state, /{set}, /healthz", func(t *testing.T) {
		if s, b := e.do("GET", "/", nil); s != 200 || len(b["flag_sets"].([]any)) != 9 {
			t.Fatalf("%d %v", s, b)
		}
		if s, b := e.do("GET", "/state", nil); s != 200 || b["epoch"] != "e1" || b["sync_streams"] != 1.0 {
			t.Fatalf("%d %v", s, b)
		}
		s, b := e.do("GET", "/agent", nil)
		if s != 200 || b["flag_set"] != "agent" || len(b["flags"].([]any)) == 0 {
			t.Fatalf("%d", s)
		}
		if s, b := e.do("GET", "/healthz", nil); s != 200 || b["status"] != "ok" {
			t.Fatalf("%d %v", s, b)
		}
	})
	t.Run("UC-053/A1 a stage reset clears that writer's overlays", func(t *testing.T) {
		e.do("PUT", "/agent/decision.backend", map[string]any{"variant": "jev", "writer": "stage"})
		if s, _ := e.do("DELETE", "/agent?writer=stage", nil); s != 204 {
			t.Fatal(s)
		}
		_, b := e.do("GET", "/agent/decision.backend", nil)
		if b["effective_default"] != "rules" {
			t.Fatalf("%v", b["effective_default"])
		}
	})
}

// Metric labels carry only baseline keys: a request for an unknown set or flag
// is counted under "unknown", so a client cannot grow the label set (PR #1417
// review).
//
// UC: UC-053 A5 BR-005
func TestWriteMetricLabelsAreBounded(t *testing.T) {
	t.Run("UC-053/A5 unknown sets and flags are labelled unknown", func(t *testing.T) {
		e := newEnv(t, true)
		for i := 0; i < 3; i++ {
			e.do("PUT", fmt.Sprintf("/nope%d/x%d", i, i), map[string]any{"variant": "a"})
			e.do("PUT", fmt.Sprintf("/agent/nope%d", i), map[string]any{"variant": "a"})
		}
		var rm metricdata.ResourceMetrics
		_ = e.reader.Collect(context.Background(), &rm)
		seen := map[string]bool{}
		for _, sm := range rm.ScopeMetrics {
			for _, m := range sm.Metrics {
				if m.Name != "flagservice_writes_total" {
					continue
				}
				for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
					set, _ := dp.Attributes.Value("flag_set")
					flag, _ := dp.Attributes.Value("flag")
					seen[set.AsString()+"/"+flag.AsString()] = true
				}
			}
		}
		if len(seen) != 2 || !seen["unknown/unknown"] || !seen["agent/unknown"] {
			t.Fatalf("label pairs %v, want only unknown/unknown and agent/unknown", seen)
		}
	})
}

// UC: UC-042 main
func TestDryRunEndpoint(t *testing.T) {
	t.Run("UC-042/main PUT ?dry_run=true answers the rendered flag and applies nothing", func(t *testing.T) {
		e := newEnv(t, true)
		body := map[string]any{"value": 4, "scope": map[string]any{"experiment_id": "exp_1", "arm": "experimental"}}
		status, res := e.do("PUT", "/game/nonstop.respawn_seconds?dry_run=true", body)
		if status != 200 || res["dry_run"] != true || res["rendered"] == nil || res["rendered_before"] == nil {
			t.Fatalf("%d %v", status, res)
		}
		_, view := e.do("GET", "/game/nonstop.respawn_seconds", nil)
		if fmt.Sprint(view["rendered"]) != fmt.Sprint(res["rendered_before"]) {
			t.Fatalf("the dry run was applied:\n%v\n%v", view["rendered"], res["rendered_before"])
		}
	})
}

// UC: UC-043 A17
func TestGuardedRealDelete(t *testing.T) {
	const path = "/game/shield.energy_max_seconds"
	setup := func(t *testing.T) *env {
		e := newEnv(t, true)
		if s, b := e.do("PUT", path, map[string]any{"value": 4, "scope": map[string]any{"game_kind": "real"}, "writer": "promotion"}); s != 200 {
			t.Fatalf("real write: %d %v", s, b)
		}
		if s, _ := e.do("PUT", "/games/g-real", map[string]any{"coordinator_instance": "c1", "game_kind": "real"}); s != 200 {
			t.Fatalf("register: %d", s)
		}
		return e
	}
	t.Run("UC-043/A17 DELETE ?unless_real_game=true is 409 real_game_live while a real game is registered", func(t *testing.T) {
		e := setup(t)
		if s, b := e.do("DELETE", path+"?game_kind=real&writer=promotion&unless_real_game=true", nil); s != 409 || b["error"] != "real_game_live" {
			t.Fatalf("%d %v, want 409 real_game_live", s, b)
		}
		if e.writes("rejected", "real_game_live") != 1 {
			t.Fatalf("the refusal is not counted as rejected/real_game_live")
		}
	})
	t.Run("UC-043/A17 a plain DELETE (the kill switch) is 204 while a real game is registered", func(t *testing.T) {
		e := setup(t)
		if s, b := e.do("DELETE", path+"?game_kind=real&writer=promotion", nil); s != 204 {
			t.Fatalf("%d %v, want 204", s, b)
		}
	})
	t.Run("UC-043/A17 the guarded DELETE is 204 once the real game ended", func(t *testing.T) {
		e := setup(t)
		e.do("DELETE", "/games/g-real", nil)
		if s, b := e.do("DELETE", path+"?game_kind=real&writer=promotion&unless_real_game=true", nil); s != 204 {
			t.Fatalf("%d %v, want 204", s, b)
		}
	})
	t.Run("UC-043/A17 a non-boolean unless_real_game is 400 malformed", func(t *testing.T) {
		e := setup(t)
		if s, b := e.do("DELETE", path+"?game_kind=real&unless_real_game=yes-please", nil); s != 400 || b["error"] != "malformed" {
			t.Fatalf("%d %v, want 400 malformed", s, b)
		}
	})
}

// UC: UC-043 A18
func TestIfWriterDelete(t *testing.T) {
	const path = "/game/shield.energy_max_seconds"
	setup := func(t *testing.T, writer string) *env {
		e := newEnv(t, true)
		if s, b := e.do("PUT", path, map[string]any{"value": 4, "scope": map[string]any{"game_kind": "real"}, "writer": writer}); s != 200 {
			t.Fatalf("real write: %d %v", s, b)
		}
		return e
	}
	realWriter := func(e *env) any {
		_, view := e.do("GET", path, nil)
		ro, _ := view["real_overlay"].(map[string]any)
		if ro == nil {
			return nil
		}
		return ro["writer"]
	}
	t.Run("UC-043/A18 DELETE ?if_writer=promotion clears the promotion's real overlay (204)", func(t *testing.T) {
		e := setup(t, "promotion")
		if s, b := e.do("DELETE", path+"?game_kind=real&if_writer=promotion", nil); s != 204 {
			t.Fatalf("%d %v, want 204", s, b)
		}
		if w := realWriter(e); w != nil {
			t.Fatalf("real overlay of %v is still there", w)
		}
	})
	t.Run("UC-043/A18 DELETE ?if_writer=promotion on another writer's overlay is 412 writer_mismatch and changes nothing", func(t *testing.T) {
		e := setup(t, "operator")
		if s, b := e.do("DELETE", path+"?game_kind=real&writer=promotion&if_writer=promotion", nil); s != 412 || b["error"] != "writer_mismatch" {
			t.Fatalf("%d %v, want 412 writer_mismatch", s, b)
		}
		if w := realWriter(e); w != "operator" {
			t.Fatalf("real overlay writer = %v after a refused DELETE, want operator", w)
		}
		if e.writes("rejected", "writer_mismatch") != 1 {
			t.Fatalf("the refusal is not counted as rejected/writer_mismatch")
		}
	})
	t.Run("UC-043/A18 the informational writer label alone is never compared", func(t *testing.T) {
		e := setup(t, "operator")
		if s, b := e.do("DELETE", path+"?game_kind=real&writer=promotion", nil); s != 204 {
			t.Fatalf("%d %v, want 204", s, b)
		}
	})
	t.Run("UC-043/A18 an empty if_writer is 400 malformed", func(t *testing.T) {
		e := setup(t, "operator")
		if s, b := e.do("DELETE", path+"?game_kind=real&if_writer=", nil); s != 400 || b["error"] != "malformed" {
			t.Fatalf("%d %v, want 400 malformed", s, b)
		}
		if w := realWriter(e); w != "operator" {
			t.Fatalf("real overlay writer = %v, want operator", w)
		}
	})
	t.Run("UC-043/A18 if_writer outside scope real is 400 malformed", func(t *testing.T) {
		e := setup(t, "operator")
		if s, b := e.do("DELETE", path+"?if_writer=promotion", nil); s != 400 || b["error"] != "malformed" {
			t.Fatalf("%d %v, want 400 malformed", s, b)
		}
	})
	t.Run("UC-043/A18 with unless_real_game: 409 real_game_live first, then 412 once no real game runs", func(t *testing.T) {
		e := setup(t, "operator")
		if s, _ := e.do("PUT", "/games/g-real", map[string]any{"coordinator_instance": "c1", "game_kind": "real"}); s != 200 {
			t.Fatalf("register: %d", s)
		}
		q := path + "?game_kind=real&unless_real_game=true&if_writer=promotion"
		if s, b := e.do("DELETE", q, nil); s != 409 || b["error"] != "real_game_live" {
			t.Fatalf("%d %v, want 409 real_game_live", s, b)
		}
		e.do("DELETE", "/games/g-real", nil)
		if s, b := e.do("DELETE", q, nil); s != 412 || b["error"] != "writer_mismatch" {
			t.Fatalf("%d %v, want 412 writer_mismatch", s, b)
		}
	})
}

// UC: UC-043 A19
func TestIfWriterInPut(t *testing.T) {
	const path = "/game/shield.energy_max_seconds"
	put := func(e *env, query string, value int, writer string) (int, map[string]any) {
		return e.do("PUT", path+query, map[string]any{"value": value, "scope": map[string]any{"game_kind": "real"}, "writer": writer})
	}
	realWriter := func(e *env) any {
		_, view := e.do("GET", path, nil)
		ro, _ := view["real_overlay"].(map[string]any)
		if ro == nil {
			return nil
		}
		return ro["writer"]
	}
	t.Run("UC-043/A19 PUT ?if_writer_in=promotion, writes when no real overlay is present", func(t *testing.T) {
		e := newEnv(t, true)
		if s, b := put(e, "?if_writer_in=promotion,", 4, "promotion"); s != 200 {
			t.Fatalf("%d %v, want 200", s, b)
		}
		if w := realWriter(e); w != "promotion" {
			t.Fatalf("real overlay writer = %v, want promotion", w)
		}
	})
	t.Run("UC-043/A19 PUT ?if_writer_in=promotion, replaces the promotion's own overlay", func(t *testing.T) {
		e := newEnv(t, true)
		put(e, "", 4, "promotion")
		if s, b := put(e, "?if_writer_in=promotion,", 5, "promotion"); s != 200 {
			t.Fatalf("%d %v, want 200", s, b)
		}
	})
	t.Run("UC-043/A19 PUT ?if_writer_in=promotion, on another writer's overlay is 412 writer_mismatch and changes nothing", func(t *testing.T) {
		e := newEnv(t, true)
		put(e, "", 4, "operator")
		if s, b := put(e, "?if_writer_in=promotion,", 5, "promotion"); s != 412 || b["error"] != "writer_mismatch" {
			t.Fatalf("%d %v, want 412 writer_mismatch", s, b)
		}
		if w := realWriter(e); w != "operator" {
			t.Fatalf("real overlay writer = %v after a refused PUT, want operator", w)
		}
		if e.writes("rejected", "writer_mismatch") != 1 {
			t.Fatalf("the refusal is not counted as rejected/writer_mismatch")
		}
	})
	t.Run("UC-043/A19 without if_writer_in a PUT still writes unconditionally", func(t *testing.T) {
		e := newEnv(t, true)
		put(e, "", 4, "operator")
		if s, b := put(e, "", 5, "promotion"); s != 200 {
			t.Fatalf("%d %v, want 200", s, b)
		}
	})
	t.Run("UC-043/A19 if_writer_in outside scope real, or repeated, is 400 malformed", func(t *testing.T) {
		e := newEnv(t, true)
		if s, b := e.do("PUT", path+"?if_writer_in=promotion,", map[string]any{"value": 4, "writer": "promotion"}); s != 400 || b["error"] != "malformed" {
			t.Fatalf("stack scope: %d %v, want 400 malformed", s, b)
		}
		if s, b := put(e, "?if_writer_in=promotion&if_writer_in=", 4, "promotion"); s != 400 || b["error"] != "malformed" {
			t.Fatalf("repeated: %d %v, want 400 malformed", s, b)
		}
	})
	t.Run("UC-043/A19 409 real_game_live takes precedence over 412", func(t *testing.T) {
		e := newEnv(t, true)
		put(e, "", 4, "operator")
		if s, _ := e.do("PUT", "/games/g-real", map[string]any{"coordinator_instance": "c1", "game_kind": "real"}); s != 200 {
			t.Fatalf("register: %d", s)
		}
		if s, b := put(e, "?if_writer_in=promotion,", 5, "promotion"); s != 409 || b["error"] != "real_game_live" {
			t.Fatalf("%d %v, want 409 real_game_live", s, b)
		}
		e.do("DELETE", "/games/g-real", nil)
		if s, b := put(e, "?if_writer_in=promotion,", 5, "promotion"); s != 412 || b["error"] != "writer_mismatch" {
			t.Fatalf("%d %v, want 412 writer_mismatch", s, b)
		}
	})
}

// The read endpoints serve a row's optional description inside its policy, for
// one flag and for the whole set; a row without one carries no description key
// (#2345).
//
// UC: UC-042 BR-008
func TestReadEndpointsServeDescription(t *testing.T) {
	e := newEnvRegistry(t, false, `flags:
  - {set: game, key: invincibility_seconds, kind: overlay, scope: [stack, experiment+arm], bounds: {range: [0, 10]}, description: "seconds a fighter cannot lose at the start"}
  - {set: game, key: sensitivity, kind: overlay, scope: [stack]}
`)
	t.Run("UC-042/BR-008 GET /game/{flag} and GET /game carry policy.description", func(t *testing.T) {
		s, b := e.do("GET", "/game/invincibility_seconds", nil)
		if s != 200 {
			t.Fatalf("GET flag: %d", s)
		}
		policy, _ := b["policy"].(map[string]any)
		if d, _ := policy["description"].(string); d != "seconds a fighter cannot lose at the start" {
			t.Fatalf("policy.description = %q", d)
		}
		s, b = e.do("GET", "/game", nil)
		if s != 200 {
			t.Fatalf("GET set: %d", s)
		}
		described := 0
		for _, f := range b["flags"].([]any) {
			p, _ := f.(map[string]any)["policy"].(map[string]any)
			if d, _ := p["description"].(string); d != "" {
				described++
			}
		}
		if described != 1 {
			t.Fatalf("GET /game: %d flags with a description, want 1", described)
		}
		_, b = e.do("GET", "/game/sensitivity", nil)
		if _, ok := b["policy"].(map[string]any)["description"]; ok {
			t.Fatalf("sensitivity has no description, the key must be absent: %v", b["policy"])
		}
	})
}

// UC: UC-042 main
func TestStrictBooleanQueryParams(t *testing.T) {
	const path = "/game/nonstop.respawn_seconds"
	put := map[string]any{"value": 4, "scope": map[string]any{"experiment_id": "exp_1", "arm": "experimental"}}
	bad := []string{"yes", "True", "TRUE", "", "2", "on"}
	cases := []struct{ method, param, rest string }{
		{"PUT", "dry_run", ""},
		{"DELETE", "persist", ""},
		{"DELETE", "fraction", ""},
		{"DELETE", "unless_real_game", "game_kind=real&"},
	}
	for _, c := range cases {
		for _, v := range bad {
			t.Run(fmt.Sprintf("UC-042/main %s ?%s=%q is 400 naming the parameter", c.method, c.param, v), func(t *testing.T) {
				e := newEnv(t, true)
				var body any
				if c.method == "PUT" {
					body = put
				}
				s, b := e.do(c.method, path+"?"+c.rest+c.param+"="+v, body)
				if s != 400 || b["error"] != "malformed" || !strings.Contains(b["detail"].(string), c.param) {
					t.Fatalf("%d %v, want 400 malformed naming %s", s, b, c.param)
				}
			})
		}
	}
	t.Run("UC-042/main dry_run=1 validates and does not write; dry_run=0 writes", func(t *testing.T) {
		e := newEnv(t, true)
		_, before := e.do("GET", path, nil)
		s, res := e.do("PUT", path+"?dry_run=1", put)
		if s != 200 || res["dry_run"] != true {
			t.Fatalf("%d %v", s, res)
		}
		_, after := e.do("GET", path, nil)
		if fmt.Sprint(before["rendered"]) != fmt.Sprint(after["rendered"]) {
			t.Fatalf("dry_run=1 wrote:\n%v\n%v", before["rendered"], after["rendered"])
		}
		if s, res := e.do("PUT", path+"?dry_run=0", put); s != 200 || res["dry_run"] == true {
			t.Fatalf("dry_run=0: %d %v", s, res)
		}
		_, applied := e.do("GET", path, nil)
		if fmt.Sprint(before["rendered"]) == fmt.Sprint(applied["rendered"]) {
			t.Fatal("dry_run=0 did not write")
		}
	})
	t.Run("UC-042/main 1, 0, true and false are accepted on DELETE", func(t *testing.T) {
		for _, q := range []string{"persist=1", "persist=0", "persist=true", "persist=false", "fraction=false", "fraction=0"} {
			e := newEnv(t, true)
			if s, b := e.do("DELETE", "/game/shield.activation?"+q, nil); s != 204 {
				t.Fatalf("?%s: %d %v, want 204", q, s, b)
			}
		}
	})
}

// newEnvStateFile is newEnv (stream open) with the state file stateFile.
func newEnvStateFile(t *testing.T, stateFile string) *httptest.Server {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Join(filepath.Dir(file), "..")
	base, err := baseline.Load(filepath.Join(root, "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	reg, err := registry.LoadFile(filepath.Join(root, "registry.yaml"), base)
	if err != nil {
		t.Fatal(err)
	}
	st, _, err := state.New(base, reg, state.Options{Epoch: "e1", Served: base.Order, StateFile: stateFile, Logger: slog.New(slog.NewTextHandler(io.Discard, nil))})
	if err != nil {
		t.Fatal(err)
	}
	sub, _ := st.Subscribe("")
	t.Cleanup(sub.Close)
	metrics, err := telemetry.NewMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(sdkmetric.NewManualReader())).Meter("t"), st.Stats)
	if err != nil {
		t.Fatal(err)
	}
	a := &API{State: st, Metrics: metrics, Tracer: sdktrace.NewTracerProvider().Tracer("t"), Links: &syncserver.Links{}}
	srv := httptest.NewServer(a.Handler())
	t.Cleanup(srv.Close)
	return srv
}

func deleteHeader(t *testing.T, url string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(http.MethodDelete, url, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	return resp.StatusCode, resp.Header.Get(HeaderPersisted)
}

// UC: UC-053 A12
func TestKillSwitchPersistedClearSignalsPersistFailure(t *testing.T) {
	t.Run("UC-053/A12 a kill switch DELETE ?persist=true with an unwritable state file answers 204 and X-Flag-Persisted false (#2368)", func(t *testing.T) {
		path := filepath.Join(t.TempDir(), "admin.json")
		if err := os.Mkdir(path, 0o755); err != nil { // unreadable at start: persistence blocked
			t.Fatal(err)
		}
		srv := newEnvStateFile(t, path)
		e := &env{t: t, srv: srv}
		code, body := e.do(http.MethodPut, "/agent/interventions_allowed", map[string]any{"variant": "full", "persist": true, "writer": "menu"})
		if code != http.StatusOK || body["persisted"] != false {
			t.Fatalf("PUT %d %v, want 200 persisted false", code, body)
		}
		code, hdr := deleteHeader(t, srv.URL+"/agent/interventions_allowed?persist=true")
		if code != http.StatusNoContent || hdr != "false" {
			t.Fatalf("DELETE %d %s=%q, want 204 and false", code, HeaderPersisted, hdr)
		}
	})
	t.Run("UC-053/A2 a persisted DELETE that reached the state file answers X-Flag-Persisted true", func(t *testing.T) {
		srv := newEnvStateFile(t, filepath.Join(t.TempDir(), "admin.json"))
		e := &env{t: t, srv: srv}
		if code, body := e.do(http.MethodPut, "/agent/interventions_allowed", map[string]any{"variant": "full", "persist": true, "writer": "menu"}); code != http.StatusOK || body["persisted"] != true {
			t.Fatalf("PUT %d %v", code, body)
		}
		code, hdr := deleteHeader(t, srv.URL+"/agent/interventions_allowed?persist=true")
		if code != http.StatusNoContent || hdr != "true" {
			t.Fatalf("DELETE %d %s=%q, want 204 and true", code, HeaderPersisted, hdr)
		}
	})
}
