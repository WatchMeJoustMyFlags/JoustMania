package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// The dossier decision follows how the experiment ended, not only its verdict
// (#1731): a promote verdict whose promotion could not act ends `discarded`, and
// an aborted experiment is never shown as promoted.
// UC: UC-035 A1
func TestDossierDecisionFollowsTerminalStatus_1731(t *testing.T) {
	cases := []struct {
		name     string
		verdict  string // "" = no verdict was reached
		terminal string
		want     string
	}{
		{"promote verdict, promotion acted (done)", "promote", "done", "promoted"},
		{"promote verdict, inert promotion (discarded)", "promote", "discarded", "discarded"},
		{"promote verdict, aborted during promotion", "promote", "aborted", "aborted"},
		{"discard verdict, discarded", "discard", "discarded", "discarded"},
		{"inconclusive verdict, discarded", "inconclusive", "discarded", "refined"},
		{"no verdict, aborted", "", "aborted", "aborted"},
		{"no verdict, discarded", "", "discarded", "discarded"},
	}
	for _, c := range cases {
		t.Run("UC-035/A1 "+c.name+" reads "+c.want, func(t *testing.T) {
			root := t.TempDir()
			id := seedJournal(t, root, "exp_end00001", false)
			j, err := journal.Load(root, id)
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			at := time.Date(2026, 6, 16, 12, 10, 0, 0, time.UTC)
			if c.verdict != "" {
				if err := j.AppendEvent(journal.Event{
					At: at, Kind: journal.KindOutcome, Status: "concluded",
					Verdict: &journal.Verdict{Outcome: c.verdict},
				}); err != nil {
					t.Fatalf("outcome: %v", err)
				}
			}
			if err := j.AppendEvent(journal.Event{
				At: at.Add(time.Minute), Kind: journal.KindDecision, Status: c.terminal,
			}); err != nil {
				t.Fatalf("decision: %v", err)
			}

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
			if d.Decision != c.want {
				t.Errorf("decision = %q, want %q (status %q)", d.Decision, c.want, d.Status)
			}
		})
	}
}

// UC: UC-035 A1
// UC: UC-043 A17
func TestDossierDecisionEdgeCases_1869(t *testing.T) {
	at := time.Date(2026, 6, 16, 12, 10, 0, 0, time.UTC)
	outcome := func(v string) journal.Event {
		return journal.Event{At: at, Kind: journal.KindOutcome, Status: "concluded", Verdict: &journal.Verdict{Outcome: v}}
	}
	decision := func(status string, min int) journal.Event {
		return journal.Event{At: at.Add(time.Duration(min) * time.Minute), Kind: journal.KindDecision, Status: status}
	}
	promotion := func(outcome string, min int) journal.Event {
		return journal.Event{
			At: at.Add(time.Duration(min) * time.Minute), Kind: journal.KindPromotion, Status: outcome,
			Promotion: &journal.Promotion{Outcome: outcome},
		}
	}
	cases := []struct {
		name       string
		events     []journal.Event
		wantStatus string
		want       string
	}{
		{"promotion applied then reverted by the kill switch (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("applied", 2), decision("done", 3), promotion("reverted", 4)},
			"done", "reverted"},
		{"promotion reverted by the watch before the end (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("reverted", 2), decision("done", 3)},
			"done", "reverted"},
		{"promotion applied, never reverted (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("applied", 2), decision("done", 3)},
			"done", "promoted"},
		{"UC-043 A17 hold-key write still waits (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("deferred", 2), decision("done", 3)},
			"done", "deferred"},
		{"UC-043 A17 waiting write dropped by the kill switch (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("deferred", 2), decision("done", 3), promotion("discarded", 4)},
			"done", "discarded"},
		{"UC-043 A17 waiting write applied after the game (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("deferred", 2), decision("done", 3), promotion("applied", 4)},
			"done", "promoted"},
		{"UC-043 A17 waiting write applied then reverted (done)",
			[]journal.Event{outcome("promote"), decision("promoting", 1), promotion("deferred", 2), decision("done", 3), promotion("applied", 4), promotion("reverted", 5)},
			"done", "reverted"},
		{"legacy journal left at concluded keeps the verdict label",
			[]journal.Event{outcome("promote")}, "concluded", "promoted"},
		{"promotion in progress",
			[]journal.Event{outcome("promote"), decision("promoting", 1)}, "promoting", ""},
	}
	for _, c := range cases {
		t.Run("UC-035/A1 "+c.name+" reads "+c.want, func(t *testing.T) {
			root := t.TempDir()
			id := seedJournal(t, root, "exp_edge0001", false)
			j, err := journal.Load(root, id)
			if err != nil {
				t.Fatalf("Load: %v", err)
			}
			for _, e := range c.events {
				if err := j.AppendEvent(e); err != nil {
					t.Fatalf("append %s: %v", e.Kind, err)
				}
			}
			reader := &dossierReader{root: root}
			rec := httptest.NewRecorder()
			reader.handleDossier(rec, httptest.NewRequest(http.MethodGet, "/experiments/"+id, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("status = %d; body=%s", rec.Code, rec.Body.String())
			}
			var d dossierView
			if err := json.Unmarshal(rec.Body.Bytes(), &d); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if d.Status != c.wantStatus {
				t.Errorf("status = %q, want %q", d.Status, c.wantStatus)
			}
			if d.Decision != c.want {
				t.Errorf("decision = %q, want %q", d.Decision, c.want)
			}
		})
	}
}
