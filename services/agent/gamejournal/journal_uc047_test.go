package gamejournal

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/joustmania/agent/gamecontext"
)

func fp(v float64) *float64 { return &v }
func bp(v bool) *bool       { return &v }

var gameEnd = time.Date(2026, time.November, 1, 20, 0, 30, 0, time.UTC)

// endedGame is a 20 s real FFA game of three players ending at gameEnd: "a" and
// "b" move (variance 0.04 and 0.01), "c" lies still and is eliminated at 10 s.
func endedGame(id, kind string) gamecontext.GameContext {
	start := gameEnd.Add(-20 * time.Second)
	player := func(serial string, intensity, variance float64, seconds int) *gamecontext.PlayerSignals {
		p := &gamecontext.PlayerSignals{Serial: serial, MovementVariance: fp(variance)}
		for i := 1; i <= seconds; i++ {
			p.History = append(p.History, gamecontext.MovementSample{At: start.Add(time.Duration(i) * time.Second), Intensity: intensity, Variance: fp(variance)})
			p.Game.IntensitySum += intensity
			p.Game.IntensitySamples++
			p.Game.VarianceSum += variance
			p.Game.VarianceSamples++
		}
		return p
	}
	return gamecontext.GameContext{
		SessionID: id,
		GameKind:  kind,
		EndReason: gamecontext.EndReasonZero,
		Session: gamecontext.SessionSignals{
			GameActive:          bp(false),
			GameMode:            func() *string { s := "JoustFFA"; return &s }(),
			DurationSeconds:     fp(20),
			DeathThreshold:      fp(2),
			EliminationSequence: []string{"c"},
		},
		Players: map[string]*gamecontext.PlayerSignals{
			"a": player("a", 1.0, 0.04, 20),
			"b": player("b", 0.6, 0.01, 20),
			"c": player("c", 0.2, 0.0001, 10),
		},
		Timeline: []gamecontext.TimelineEvent{
			{At: start.Add(10 * time.Second), Kind: gamecontext.EventElimination, Serial: "c"},
			{At: gameEnd, Kind: gamecontext.EventPhase, Detail: "end"},
		},
	}
}

// runRecorder feeds games to a recorder over j and waits for its writer to drain.
func runRecorder(t *testing.T, r *Recorder, games ...gamecontext.GameContext) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { r.Run(ctx); close(done) }()
	for _, g := range games {
		r.OnGameEnd(g)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("recorder did not drain")
	}
}

func failureCount(t *testing.T, reader *sdkmetric.ManualReader, reason string) int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect: %v", err)
	}
	for _, sm := range rm.ScopeMetrics {
		for _, md := range sm.Metrics {
			if md.Name != MetricWriteFailures {
				continue
			}
			for _, dp := range md.Data.(metricdata.Sum[int64]).DataPoints {
				if v, ok := dp.Attributes.Value("reason"); ok && v.AsString() == reason {
					return dp.Value
				}
			}
		}
	}
	return 0
}

func fileLines(t *testing.T, path string) []map[string]any {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open journal: %v", err)
	}
	defer func() { _ = f.Close() }()
	var out []map[string]any
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		var m map[string]any
		if err := json.Unmarshal(sc.Bytes(), &m); err != nil {
			t.Fatalf("journal line is not JSON: %v: %s", err, sc.Text())
		}
		out = append(out, m)
	}
	return out
}

func near(p *float64, want float64) bool { return p != nil && math.Abs(*p-want) < 1e-9 }

// UC: UC-047 main step 1 BR-009 BR-002 BR-003 BR-006
func TestRecorder_RealGameEndWritesOneRecord(t *testing.T) {
	root := t.TempDir()
	j, _, err := Open(root, 0)
	if err != nil {
		t.Fatal(err)
	}
	r := NewRecorder(j, func(context.Context) (bool, bool) { return true, true }, nil, nil)
	g := endedGame("game_1", gamecontext.GameKindReal)
	runRecorder(t, r, g, g) // the same game twice is journaled once

	lines := fileLines(t, filepath.Join(root, "calibration", "real_games.jsonl"))
	if len(lines) != 1 {
		t.Fatalf("journal has %d lines, want 1", len(lines))
	}
	for _, key := range []string{"schema_version", "game_id", "mode", "ended_at", "duration_seconds", "test_session",
		"eliminations", "players", "history_truncated", "metrics"} {
		if _, ok := lines[0][key]; !ok {
			t.Errorf("record has no %q field: %v", key, lines[0])
		}
	}
	if strings.Contains(fmt.Sprint(lines[0]), "serial") {
		t.Errorf("C-009: a record must not name a serial: %v", lines[0])
	}

	rec := j.Records()[0]
	if rec.GameID != "game_1" || rec.Mode != "JoustFFA" || !rec.EndedAt.Equal(gameEnd) || !near(rec.DurationSeconds, 20) {
		t.Errorf("identity fields = %+v", rec)
	}
	if rec.TestSession == nil || !*rec.TestSession {
		t.Errorf("test_session = %v, want true", rec.TestSession)
	}
	if rec.Eliminations != 1 || len(rec.Players) != 3 || rec.HistoryTruncated {
		t.Fatalf("eliminations=%d players=%d truncated=%v", rec.Eliminations, len(rec.Players), rec.HistoryTruncated)
	}
	// Players are sorted by mean variance, highest first, without a serial.
	if !near(rec.Players[0].MeanMovementVariance, 0.04) || !near(rec.Players[1].MeanMovementVariance, 0.01) ||
		!near(rec.Players[2].MeanMovementVariance, 0.0001) || !rec.Players[2].Eliminated || rec.Players[0].Eliminated {
		t.Errorf("players = %+v", rec.Players)
	}
	if rec.Players[0].VarianceSamples != 20 || !near(rec.Players[0].MeanIntensity, 1.0) || !near(rec.Players[0].LastMovementVariance, 0.04) {
		t.Errorf("player a = %+v", rec.Players[0])
	}
	m := rec.Metrics
	if !near(m.Intensity, 0.6) || !near(m.MovementVariance, (0.04+0.01+0.0001)/3) || !near(m.EliminationPace, 3) {
		t.Errorf("metrics = intensity %v variance %v pace %v", m.Intensity, m.MovementVariance, m.EliminationPace)
	}
	if m.NearDeathRate != nil {
		t.Errorf("near_death_rate = %v, want null (not ingested by the agent)", *m.NearDeathRate)
	}
	// Field activity (intensity / threshold 2): seconds 1..9 (0.5+0.3+0.1)/3 = 0.3;
	// seconds 10..20, "c" out at 10: (0.5+0.3)/2 = 0.4; second 0 has no sample.
	// activity = (9*0.3 + 11*0.4) / 20 = 0.355.
	if m.Activity == nil || !near(m.Activity, 0.355) {
		t.Errorf("activity = %v, want 0.355", m.Activity)
	}
	// Sub-window means 0.3, 0.32, 0.4, 0.4: swings 0.1 (t0+15) and 0.08 (t0+20).
	if m.Change == nil || !near(m.Change, 0.09) {
		t.Errorf("change = %v, want 0.09", m.Change)
	}
}

// UC: UC-047 BR-003
func TestRecorder_UnreadableTestSessionIsNull(t *testing.T) {
	j, _, _ := Open(t.TempDir(), 0)
	r := NewRecorder(j, func(context.Context) (bool, bool) { return false, false }, nil, nil)
	runRecorder(t, r, endedGame("game_1", gamecontext.GameKindReal))
	if got := j.Records(); len(got) != 1 || got[0].TestSession != nil {
		t.Fatalf("records = %+v, want one with test_session null", got)
	}
}

// UC: UC-047 main step 1 BR-009
func TestRecorder_ShadowGameWritesNone(t *testing.T) {
	for _, kind := range []string{gamecontext.GameKindShadow, ""} {
		t.Run(fmt.Sprintf("UC-047/BR-009 kind %q is not journaled", kind), func(t *testing.T) {
			root := t.TempDir()
			j, _, _ := Open(root, 0)
			r := NewRecorder(j, nil, nil, nil)
			runRecorder(t, r, endedGame("game_shadow", kind))
			if n := len(j.Records()); n != 0 {
				t.Fatalf("journal holds %d records, want 0", n)
			}
			if _, err := os.Stat(filepath.Join(root, "calibration", "real_games.jsonl")); !errors.Is(err, os.ErrNotExist) {
				t.Fatalf("journal file exists after a %q game: %v", kind, err)
			}
		})
	}
	t.Run("UC-047/BR-009 a still-active snapshot is not journaled", func(t *testing.T) {
		j, _, _ := Open(t.TempDir(), 0)
		g := endedGame("game_live", gamecontext.GameKindReal)
		g.Session.GameActive = bp(true)
		runRecorder(t, NewRecorder(j, nil, nil, nil), g)
		if n := len(j.Records()); n != 0 {
			t.Fatalf("journal holds %d records, want 0", n)
		}
	})
}

// UC: UC-047 BR-009
func TestJournal_RotatesAtMax(t *testing.T) {
	root := t.TempDir()
	j, _, _ := Open(root, 3)
	for i := 1; i <= 5; i++ {
		if err := j.Append(BuildRecord(endedGame(fmt.Sprintf("game_%d", i), gamecontext.GameKindReal), nil, gameEnd)); err != nil {
			t.Fatal(err)
		}
	}
	ids := func(rs []Record) string {
		var out []string
		for _, r := range rs {
			out = append(out, r.GameID)
		}
		return strings.Join(out, ",")
	}
	if got := ids(j.Records()); got != "game_3,game_4,game_5" {
		t.Fatalf("records = %s, want the newest 3", got)
	}
	if n := len(fileLines(t, j.Path())); n != 3 {
		t.Fatalf("file has %d lines, want 3", n)
	}
	// A restart reloads the bounded file; an evicted id can be journaled again.
	j2, rep, err := Open(root, 3)
	if err != nil || rep.Records != 3 || rep.Skipped != 0 || ids(j2.Records()) != "game_3,game_4,game_5" {
		t.Fatalf("reload = %s %+v %v", ids(j2.Records()), rep, err)
	}
	if DefaultMaxGames != 500 {
		t.Errorf("DefaultMaxGames = %d, want 500 (UC-047 BR-009)", DefaultMaxGames)
	}
}

// UC: UC-047 BR-009
func TestRecorder_WriteErrorIsFailSoft(t *testing.T) {
	t.Run("UC-047/BR-009 an unwritable volume only counts and logs", func(t *testing.T) {
		root := t.TempDir()
		reader := sdkmetric.NewManualReader()
		j, _, err := Open(root, 0)
		if err != nil {
			t.Fatal(err)
		}
		// The volume breaks after start: a file where the journal directory goes.
		if err := os.WriteFile(filepath.Join(root, Dir), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		r := NewRecorder(j, nil, sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)), nil)
		runRecorder(t, r, endedGame("game_1", gamecontext.GameKindReal), endedGame("game_2", gamecontext.GameKindReal))
		if got := failureCount(t, reader, ReasonWrite); got != 2 {
			t.Fatalf("%s{reason=write} = %d, want 2", MetricWriteFailures, got)
		}
		// The records stay in memory for the next successful write.
		if n := len(j.Records()); n != 2 {
			t.Fatalf("in-memory records = %d, want 2", n)
		}
	})
	t.Run("UC-047/BR-009 a full queue drops the record without blocking", func(t *testing.T) {
		reader := sdkmetric.NewManualReader()
		j, _, _ := Open(t.TempDir(), 0)
		r := NewRecorder(j, nil, sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader)), nil)
		done := make(chan struct{})
		go func() { // no writer runs: OnGameEnd must still return
			for i := 0; i < queueSize+2; i++ {
				r.OnGameEnd(endedGame(fmt.Sprintf("game_%d", i), gamecontext.GameKindReal))
			}
			close(done)
		}()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Fatal("OnGameEnd blocked on a full queue")
		}
		if got := failureCount(t, reader, ReasonQueueFull); got != 2 {
			t.Fatalf("%s{reason=queue_full} = %d, want 2", MetricWriteFailures, got)
		}
	})
	t.Run("UC-047/BR-009 an unreadable file is never replaced", func(t *testing.T) {
		root := t.TempDir()
		path := filepath.Join(root, Dir, File)
		if err := os.MkdirAll(path, 0o755); err != nil { // a directory where the file should be: unreadable as a file
			t.Fatal(err)
		}
		j, _, err := Open(root, 0)
		if err == nil {
			t.Fatal("Open of an unreadable journal must report an error")
		}
		if err := j.Append(BuildRecord(endedGame("game_1", gamecontext.GameKindReal), nil, gameEnd)); err == nil {
			t.Fatal("Append after an unreadable load must refuse, not replace the file")
		}
		if fi, err := os.Stat(path); err != nil || !fi.IsDir() {
			t.Fatalf("journal path was replaced: %v", err)
		}
	})
	t.Run("UC-047/BR-009 invalid lines are skipped on load", func(t *testing.T) {
		root := t.TempDir()
		j, _, _ := Open(root, 0)
		_ = j.Append(BuildRecord(endedGame("game_ok", gamecontext.GameKindReal), nil, gameEnd))
		f, _ := os.OpenFile(j.Path(), os.O_APPEND|os.O_WRONLY, 0o644)
		_, _ = f.WriteString("{torn\n{\"schema_version\":99,\"game_id\":\"x\"}\n")
		_ = f.Close()
		j2, rep, err := Open(root, 0)
		if err != nil || rep.Records != 1 || rep.Skipped != 2 || len(j2.Records()) != 1 {
			t.Fatalf("load = %+v %v", rep, err)
		}
	})
}

// UC: UC-047 BR-008 BR-009
func TestJournal_AtomicWrite(t *testing.T) {
	root := t.TempDir()
	j, _, _ := Open(root, 0)
	if err := j.Append(BuildRecord(endedGame("game_1", gamecontext.GameKindReal), nil, gameEnd)); err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(j.Path())
	if err != nil {
		t.Fatal(err)
	}

	// A failure at the rename leaves the previous file byte-identical and no
	// temporary file behind: a reader never sees a partial journal.
	var renamedFrom string
	j.rename = func(oldpath, _ string) error {
		renamedFrom = oldpath
		return errors.New("injected rename failure")
	}
	if err := j.Append(BuildRecord(endedGame("game_2", gamecontext.GameKindReal), nil, gameEnd)); err == nil {
		t.Fatal("Append must report the failed rename")
	}
	if filepath.Dir(renamedFrom) != filepath.Dir(j.Path()) {
		t.Errorf("temporary file %q is not in the journal directory (rename must stay on one filesystem)", renamedFrom)
	}
	after, _ := os.ReadFile(j.Path())
	if string(after) != string(before) {
		t.Fatal("a failed write changed the journal file")
	}
	entries, _ := os.ReadDir(filepath.Dir(j.Path()))
	if len(entries) != 1 {
		t.Fatalf("journal dir holds %d entries after a failed write, want only the journal", len(entries))
	}

	// The next write succeeds and carries the record the failed one kept in memory.
	j.rename = os.Rename
	if err := j.Append(BuildRecord(endedGame("game_3", gamecontext.GameKindReal), nil, gameEnd)); err != nil {
		t.Fatal(err)
	}
	if n := len(fileLines(t, j.Path())); n != 3 {
		t.Fatalf("file has %d lines, want 3", n)
	}
	entries, _ = os.ReadDir(filepath.Dir(j.Path()))
	if len(entries) != 1 {
		t.Fatalf("journal dir holds %d entries after a write, want only the journal", len(entries))
	}
}

// UC: UC-047 BR-006 BR-009
func TestBuildRecord_HistoryTruncatedAndMissingThreshold(t *testing.T) {
	g := endedGame("game_long", gamecontext.GameKindReal)
	g.Session.DurationSeconds = fp(60) // the 20 s history now starts long after the start
	a := g.Players["a"]
	for len(a.History) < gamecontext.PlayerHistoryCap {
		a.History = append(a.History, a.History[len(a.History)-1])
	}
	if rec := BuildRecord(g, nil, gameEnd); !rec.HistoryTruncated {
		t.Error("history_truncated = false for a full ring that starts after the game start")
	}
	g = endedGame("game_nothreshold", gamecontext.GameKindReal)
	g.Session.DeathThreshold = nil
	if rec := BuildRecord(g, nil, gameEnd); rec.Metrics.Activity != nil || rec.Metrics.Change != nil {
		t.Errorf("activity/change without a threshold = %v/%v, want null", rec.Metrics.Activity, rec.Metrics.Change)
	}
}
