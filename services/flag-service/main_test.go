package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/joustmania/flag-service/baseline"
	"github.com/joustmania/flag-service/registry"
	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/telemetry"
)

func loadFixtures(t *testing.T) (*baseline.Baseline, *registry.Registry) {
	t.Helper()
	_, file, _, _ := runtime.Caller(0)
	root := filepath.Dir(file)
	base, err := baseline.Load(filepath.Join(root, "..", "flagd", "ci"), baseline.DefaultFiles)
	if err != nil {
		t.Fatal(err)
	}
	reg, err := registry.LoadFile(filepath.Join(root, "registry.yaml"), base)
	if err != nil {
		t.Fatal(err)
	}
	return base, reg
}

func counterTotal(t *testing.T, reader *sdkmetric.ManualReader, name string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	var n int64
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != name {
				continue
			}
			for _, dp := range m.Data.(metricdata.Sum[int64]).DataPoints {
				n += dp.Value
			}
		}
	}
	return n
}

type sweepEnv struct {
	st     *state.State
	now    time.Time
	logs   *bytes.Buffer
	logger *slog.Logger
	reader *sdkmetric.ManualReader
	spans  *tracetest.SpanRecorder
	m      *telemetry.Metrics
}

func newSweepEnv(t *testing.T) *sweepEnv {
	t.Helper()
	base, reg := loadFixtures(t)
	var err error
	e := &sweepEnv{now: time.Date(2026, 10, 19, 12, 0, 0, 0, time.UTC), logs: &bytes.Buffer{}}
	e.logger = slog.New(slog.NewJSONHandler(e.logs, nil))
	e.st, _, err = state.New(base, reg, state.Options{
		Epoch: "e1", Served: base.Order, Logger: e.logger,
		Now: func() time.Time { return e.now },
	})
	if err != nil {
		t.Fatal(err)
	}
	e.reader = sdkmetric.NewManualReader()
	e.m, err = telemetry.NewMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(e.reader)).Meter("t"), e.st.Stats)
	if err != nil {
		t.Fatal(err)
	}
	e.spans = tracetest.NewSpanRecorder()
	return e
}

func (e *sweepEnv) sweep() {
	tracer := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(e.spans)).Tracer("t")
	sweepOnce(context.Background(), e.st, e.m, tracer, e.logger)
}

func (e *sweepEnv) expiredTotal(t *testing.T) int64 {
	t.Helper()
	return counterTotal(t, e.reader, "flagservice_game_scopes_expired_total")
}

// expiryWarnings returns the game ids of the WARN "game scope expired" records.
func (e *sweepEnv) expiryWarnings(t *testing.T) []string {
	t.Helper()
	var ids []string
	for _, line := range strings.Split(strings.TrimSpace(e.logs.String()), "\n") {
		if line == "" {
			continue
		}
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		if rec["level"] == "WARN" && strings.Contains(rec["msg"].(string), "game scope expired") {
			ids = append(ids, rec["game_id"].(string))
		}
	}
	return ids
}

// UC: UC-053 A11
func TestSweep_ExpiresGamesWhoseEndWasNeverReported(t *testing.T) {
	t.Run("UC-053/A11 the sweep drops a scope past the maximum game age, logs a warning and counts it", func(t *testing.T) {
		e := newSweepEnv(t)
		for _, id := range []string{"game_old", "game_new"} {
			if _, err := e.st.RegisterGame(state.Game{ID: id, CoordinatorInstance: "c/1", Kind: "real", Serials: []string{"S-" + id}}); err != nil {
				t.Fatalf("register %s: %v", id, err)
			}
			e.now = e.now.Add(state.DefaultMaxGameAge / 2)
		}
		// game_old is now 1 h old, game_new 30 min: one more minute expires only game_old.
		e.now = e.now.Add(time.Minute)

		e.sweep()

		if e.st.Game("game_old") != nil {
			t.Fatal("game_old outlived the maximum game age")
		}
		if e.st.Game("game_new") == nil {
			t.Fatal("game_new expired before the maximum game age")
		}
		if got := e.expiryWarnings(t); len(got) != 1 || got[0] != "game_old" {
			t.Fatalf("expiry warnings for %v, want [game_old]", got)
		}
		if n := e.expiredTotal(t); n != 1 {
			t.Fatalf("flagservice_game_scopes_expired_total = %d, want 1", n)
		}
	})

	t.Run("UC-053/A11 a sweep with no expired scope logs and counts nothing", func(t *testing.T) {
		e := newSweepEnv(t)
		if _, err := e.st.RegisterGame(state.Game{ID: "g", CoordinatorInstance: "c/1", Kind: "real", Serials: []string{"S1"}}); err != nil {
			t.Fatal(err)
		}
		e.sweep()
		if e.st.Game("g") == nil || len(e.expiryWarnings(t)) != 0 || e.expiredTotal(t) != 0 {
			t.Fatalf("a young game was expired: warnings=%v total=%d", e.expiryWarnings(t), e.expiredTotal(t))
		}
	})
}

// UC: UC-053 A12
func TestRecordLoadReport_CountsACorruptStateFile(t *testing.T) {
	for _, tc := range []struct {
		name    string
		content string
		want    int64
	}{
		{"UC-053/A12 a corrupt state file increments flagservice_admin_state_corrupt_total", "{not json", 1},
		{"UC-053/A12 a valid state file counts nothing", `{"version":1,"values":[]}`, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			base, reg := loadFixtures(t)
			path := filepath.Join(t.TempDir(), "admin.json")
			if err := os.WriteFile(path, []byte(tc.content), 0o600); err != nil {
				t.Fatal(err)
			}
			st, report, err := state.New(base, reg, state.Options{
				Epoch: "e1", Served: base.Order, StateFile: path,
				Logger: slog.New(slog.NewTextHandler(io.Discard, nil)),
			})
			if err != nil {
				t.Fatal(err)
			}
			reader := sdkmetric.NewManualReader()
			m, err := telemetry.NewMetrics(sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)).Meter("t"), st.Stats)
			if err != nil {
				t.Fatal(err)
			}
			recordLoadReport(context.Background(), m, report)
			if n := counterTotal(t, reader, "flagservice_admin_state_corrupt_total"); n != tc.want {
				t.Fatalf("flagservice_admin_state_corrupt_total = %d, want %d", n, tc.want)
			}
		})
	}
}

// UC: UC-053 A3
func TestSweep_ExpiresIdleExperimentValues(t *testing.T) {
	t.Run("UC-053/A3 arm values no game referenced for 2 h are dropped, logged and counted", func(t *testing.T) {
		e := newSweepEnv(t)
		sub, err := e.st.Subscribe("")
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(sub.Close)
		if _, werr := e.st.Put(state.WriteRequest{Set: "game", Flag: "nonstop.respawn_seconds", Value: json.RawMessage("4"),
			Scope: state.Scope{ExperimentID: "exp_orphan", Arm: "experimental"}, Writer: "agent"}); werr != nil {
			t.Fatalf("arm write: %v", werr)
		}
		e.now = e.now.Add(state.DefaultExperimentIdle - time.Minute)
		e.sweep()
		if n := counterTotal(t, e.reader, "flagservice_experiments_expired_total"); n != 0 {
			t.Fatalf("expired %d before the idle time", n)
		}
		e.now = e.now.Add(2 * time.Minute)
		e.sweep()
		if n := counterTotal(t, e.reader, "flagservice_experiments_expired_total"); n != 1 {
			t.Fatalf("flagservice_experiments_expired_total = %d, want 1", n)
		}
		if !strings.Contains(e.logs.String(), `"level":"WARN","msg":"flag service: experiment values expired`) {
			t.Fatalf("no WARN record for the expiry:\n%s", e.logs.String())
		}
		payload, _, _ := e.st.Snapshot("")
		if strings.Contains(string(payload), "exp_orphan") {
			t.Fatal("the configuration still carries the expired experiment")
		}
	})
}
