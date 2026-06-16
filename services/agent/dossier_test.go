package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// seedJournal creates an experiment journal under root and folds a small,
// deterministic game history so the dossier has arms, games, a verdict, and an
// outcome to project. Returns the experiment id.
func seedJournal(t *testing.T, root, id string, concluded bool) string {
	t.Helper()
	base := time.Date(2026, 6, 16, 12, 0, 0, 0, time.UTC)
	intent := journal.Intent{
		ExperimentID:      id,
		CreatedAt:         base,
		Hypothesis:        "a longer death grace reduces frustration churn",
		FlagKey:           "death_grace_period_seconds",
		ExperimentalValue: 2.5,
		Objective:         "endurance",
		TargetNPerArm:     2,
		Arms:              []string{journal.ArmExperimental, journal.ArmControl},
	}
	j, err := journal.Create(root, intent)
	if err != nil {
		t.Fatalf("Create: %v", err)
	}

	pair := 0
	assign := func(gameID, arm string, at time.Time) {
		if err := j.AppendEvent(journal.Event{
			At: at, Kind: journal.KindGameAssigned, GameID: gameID, Arm: arm, PairIndex: &pair,
		}); err != nil {
			t.Fatalf("assign: %v", err)
		}
	}
	conclude := func(gameID, arm string, fitness, dur float64, at time.Time) {
		f := fitness
		if err := j.AppendEvent(journal.Event{
			At: at, Kind: journal.KindGameConcluded, GameID: gameID, Arm: arm, Fitness: &f, DurationS: dur,
		}); err != nil {
			t.Fatalf("conclude: %v", err)
		}
	}

	assign("game_aaa", journal.ArmExperimental, base.Add(1*time.Minute))
	conclude("game_aaa", journal.ArmExperimental, 0.8, 42.0, base.Add(2*time.Minute))
	assign("game_bbb", journal.ArmControl, base.Add(3*time.Minute))
	conclude("game_bbb", journal.ArmControl, 0.5, 40.0, base.Add(4*time.Minute))

	// A released (non-counting) game to exercise that outcome branch.
	assign("game_ccc", journal.ArmExperimental, base.Add(5*time.Minute))
	if err := j.AppendEvent(journal.Event{
		At: base.Add(6 * time.Minute), Kind: journal.KindGameReleased, GameID: "game_ccc",
		Arm: journal.ArmExperimental, Note: "game_error",
	}); err != nil {
		t.Fatalf("release: %v", err)
	}

	if concluded {
		if err := j.AppendEvent(journal.Event{
			At: base.Add(7 * time.Minute), Kind: journal.KindOutcome, Status: "concluded",
			Note:    "experimental arm cleared the floor",
			Verdict: &journal.Verdict{Outcome: "promote", Delta: 0.3, Significant: true, Reason: "delta > floor"},
		}); err != nil {
			t.Fatalf("outcome: %v", err)
		}
	}
	return id
}

func TestDossierList(t *testing.T) {
	root := t.TempDir()
	seedJournal(t, root, "exp_running01", false)
	seedJournal(t, root, "exp_done0002", true)

	reader := &dossierReader{root: root}
	rec := httptest.NewRecorder()
	reader.handleList(rec, httptest.NewRequest(http.MethodGet, "/experiments", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	var body struct {
		Experiments []experimentListItem `json:"experiments"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v; body=%s", err, rec.Body.String())
	}
	if len(body.Experiments) != 2 {
		t.Fatalf("got %d experiments, want 2", len(body.Experiments))
	}
	byID := map[string]experimentListItem{}
	for _, it := range body.Experiments {
		byID[it.ID] = it
	}
	done := byID["exp_done0002"]
	if done.Status != "concluded" {
		t.Errorf("done status = %q, want concluded", done.Status)
	}
	if done.Verdict == nil || done.Verdict.Outcome != "promote" {
		t.Errorf("done verdict = %+v, want promote", done.Verdict)
	}
	if done.ConcludedAt == nil {
		t.Errorf("done concluded_at is nil, want a timestamp")
	}
	if done.FlagKey != "death_grace_period_seconds" {
		t.Errorf("flag_key = %q", done.FlagKey)
	}
	running := byID["exp_running01"]
	if running.ConcludedAt != nil {
		t.Errorf("running concluded_at = %v, want nil", running.ConcludedAt)
	}
}

func TestDossierDetail(t *testing.T) {
	root := t.TempDir()
	id := seedJournal(t, root, "exp_done0002", true)

	reader := &dossierReader{root: root}
	rec := httptest.NewRecorder()
	reader.handleDossier(rec, httptest.NewRequest(http.MethodGet, "/experiments/"+id, nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	var d dossierView
	if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
		t.Fatalf("decode: %v", err)
	}

	if d.ID != id {
		t.Errorf("id = %q, want %q", d.ID, id)
	}
	if d.Thesis.Hypothesis == "" || d.Thesis.FlagKey != "death_grace_period_seconds" {
		t.Errorf("thesis = %+v", d.Thesis)
	}
	if d.Decision != "promoted" {
		t.Errorf("decision = %q, want promoted", d.Decision)
	}
	if d.Verdict == nil || !d.Verdict.Significant {
		t.Errorf("verdict = %+v", d.Verdict)
	}
	if len(d.Arms) != 2 {
		t.Errorf("got %d arms, want 2", len(d.Arms))
	}
	// Arms are sorted: control then experimental.
	if d.Arms[0].Name != journal.ArmControl || d.Arms[1].Name != journal.ArmExperimental {
		t.Errorf("arm order = %q,%q", d.Arms[0].Name, d.Arms[1].Name)
	}

	// Three games: two concluded (with fitness) and one released.
	if len(d.Games) != 3 {
		t.Fatalf("got %d games, want 3", len(d.Games))
	}
	var concluded, released int
	for _, g := range d.Games {
		switch g.Outcome {
		case "concluded":
			concluded++
			if g.Fitness == nil {
				t.Errorf("concluded game %s missing fitness", g.GameID)
			}
		case "released":
			released++
			if g.Note != "game_error" {
				t.Errorf("released game note = %q", g.Note)
			}
		}
		if g.TraceHint != g.GameID {
			t.Errorf("trace hint = %q, want game id %q", g.TraceHint, g.GameID)
		}
	}
	if concluded != 2 || released != 1 {
		t.Errorf("concluded=%d released=%d, want 2/1", concluded, released)
	}

	if d.TraceHints.ExperimentID != id || len(d.TraceHints.GameIDs) != 3 {
		t.Errorf("trace hints = %+v", d.TraceHints)
	}
	if d.ConcludedAt == nil {
		t.Errorf("concluded_at is nil")
	}
	if len(d.Notes) == 0 {
		t.Errorf("expected at least one outcome note")
	}
}

func TestDossierNotFound(t *testing.T) {
	reader := &dossierReader{root: t.TempDir()}
	rec := httptest.NewRecorder()
	reader.handleDossier(rec, httptest.NewRequest(http.MethodGet, "/experiments/exp_missing", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	var e map[string]string
	if err := json.Unmarshal(rec.Body.Bytes(), &e); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if e["error"] == "" {
		t.Errorf("expected an error body")
	}
}

func TestDossierEmptyIDIsNotFound(t *testing.T) {
	reader := &dossierReader{root: t.TempDir()}
	rec := httptest.NewRecorder()
	reader.handleDossier(rec, httptest.NewRequest(http.MethodGet, "/experiments/", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("trailing-slash status = %d, want 404", rec.Code)
	}
}

func TestDossierMethodNotAllowed(t *testing.T) {
	reader := &dossierReader{root: t.TempDir()}
	rec := httptest.NewRecorder()
	reader.handleList(rec, httptest.NewRequest(http.MethodPost, "/experiments", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("status = %d, want 405", rec.Code)
	}
}

func TestDossierEmptyRoot(t *testing.T) {
	// A first-run agent with no experiments root yields an empty list, not an error.
	reader := &dossierReader{root: t.TempDir() + "/does-not-exist"}
	rec := httptest.NewRecorder()
	reader.handleList(rec, httptest.NewRequest(http.MethodGet, "/experiments", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), `"experiments"`) {
		t.Errorf("body = %s", rec.Body.String())
	}
}

func TestDossierHTMLIndex(t *testing.T) {
	root := t.TempDir()
	seedJournal(t, root, "exp_done0002", true)
	reader := &dossierReader{root: root}

	rec := httptest.NewRecorder()
	reader.handleList(rec, httptest.NewRequest(http.MethodGet, "/experiments?format=html", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/html") {
		t.Errorf("content-type = %q, want text/html", ct)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "exp_done0002") || !strings.Contains(body, "/experiments/exp_done0002") {
		t.Errorf("html missing experiment link: %s", body)
	}
}

// TestDossierRoutesMountedOnHealthServer verifies the dossier routes are served by
// the agent's existing health HTTP server (no separate port), and that the server
// starts and stops cleanly with the run-loop lifecycle.
func TestDossierRoutesMountedOnHealthServer(t *testing.T) {
	root := t.TempDir()
	seedJournal(t, root, "exp_live0001", false)

	c := &serverComponents{
		healthAddr: freePort(t),
		logger:     testLogger(),
		dossier:    &dossierReader{root: root},
	}
	srv := c.startHealthServer()
	t.Cleanup(func() { _ = srv.Close() })

	// Poll until the listener is up, then hit both endpoints.
	base := "http://" + c.healthAddr
	var resp *http.Response
	var err error
	for i := 0; i < 50; i++ {
		resp, err = http.Get(base + "/experiments")
		if err == nil {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if err != nil {
		t.Fatalf("GET /experiments: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("/experiments status = %d", resp.StatusCode)
	}

	detail, err := http.Get(base + "/experiments/exp_live0001")
	if err != nil {
		t.Fatalf("GET dossier: %v", err)
	}
	defer detail.Body.Close()
	if detail.StatusCode != http.StatusOK {
		t.Fatalf("dossier status = %d", detail.StatusCode)
	}

	// /healthz still works alongside the dossier routes.
	hz, err := http.Get(base + "/healthz")
	if err != nil {
		t.Fatalf("GET /healthz: %v", err)
	}
	defer hz.Body.Close()
	if hz.StatusCode != http.StatusOK {
		t.Fatalf("/healthz status = %d", hz.StatusCode)
	}
}
