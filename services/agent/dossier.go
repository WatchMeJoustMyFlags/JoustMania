// dossier.go is the agent's external experiment-dossier HTTP view (#1183, epic
// #1181 increment 3). It serves the experiment NARRATIVE straight from the durable
// journal (#1025) — independent of Jaeger/Grafana — so a human can read "what WAS
// this experiment" without the trace/metrics stack.
//
// Two read-only endpoints, both JSON (with an optional minimal HTML index for
// human browsing of GET /experiments):
//
//	GET /experiments       → a list of every experiment with a durable journal:
//	                         id, hypothesis (thesis), flag/candidate, status,
//	                         verdict, created/concluded timestamps.
//	GET /experiments/{id}  → the full dossier: the thesis (idea/candidate/flag/
//	                         objective), arms with per-arm rolling stats, the games
//	                         run with per-game fitness + outcome, the verdict
//	                         (delta / significance / reason), the decision
//	                         (promoted/discarded/refined), and trace deep-link hints.
//
// DESIGN — disk-backed, decoupled from the live registry:
//
// The dossier reads the journal on disk via the journal package's public API
// (journal.List + journal.Load), NOT the in-memory Registry. This is deliberate:
// a CONCLUDED experiment is torn down from the registry (its slot is freed) but
// its journal — intent.json / events.jsonl / summary.json — persists on the
// mounted volume (the #831 durability guarantee). Reading from disk therefore
// surfaces the WHOLE history (live + terminal), which is exactly the "what was
// this experiment" report the maintainer asked for, and it touches NONE of the
// registry's span/lifecycle internals (PR #1182 adds an experiment span there —
// this stays clear of it).
//
// READ-ONLY + non-blocking: every handler only reads files; it holds no agent
// lock and runs on the health HTTP server's own goroutine, so it can never block
// the agent's decision/experiment loops. Errors degrade to 404 (unknown id) /
// 500 (unreadable journal) with a small JSON error body.
package main

import (
	"encoding/json"
	"errors"
	"html"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/joustmania/agent/experiment/journal"
)

// dossierReader resolves and reads experiment journals from the durable
// experiments root. It is a thin, stateless wrapper over the journal package's
// public read API so the HTTP layer stays free of filesystem details.
type dossierReader struct {
	// root is the experiments directory (AGENT_EXPERIMENT_DIR / DefaultDir).
	root string
}

// newDossierReader binds a reader to the experiments root resolved from the
// environment (the same seam the registry's journals use, so the dossier reads
// exactly the journals the agent writes).
func newDossierReader() *dossierReader {
	return &dossierReader{root: journal.DirFromEnv()}
}

// --- JSON response shapes (stable wire contract for the dossier) ---

// experimentListItem is one row of GET /experiments — the at-a-glance summary.
type experimentListItem struct {
	// ID is the stable experiment id (exp_<hex>).
	ID string `json:"id"`
	// Hypothesis is the agent's thesis: what change + why it might help.
	Hypothesis string `json:"hypothesis"`
	// FlagKey is the game flag under experiment.
	FlagKey string `json:"flag_key"`
	// Candidate is the experimental-arm value being tested (intent.experimental_value).
	Candidate any `json:"candidate"`
	// Objective is the fitness objective being optimized.
	Objective string `json:"objective"`
	// Status is the lifecycle status (running / concluded / ...).
	Status string `json:"status"`
	// Verdict is the current/terminal verdict, or nil if none recorded yet.
	Verdict *verdictView `json:"verdict,omitempty"`
	// StartedAt is when the experiment was created.
	StartedAt time.Time `json:"started_at"`
	// ConcludedAt is when the experiment reached a terminal outcome, or nil if still
	// running (derived from the first outcome event).
	ConcludedAt *time.Time `json:"concluded_at,omitempty"`
}

// verdictView is the verdict surfaced in both the list and the dossier.
type verdictView struct {
	// Outcome is the verdict label (inconclusive / promote / discard).
	Outcome string `json:"outcome"`
	// Delta is experimental mean minus control mean.
	Delta float64 `json:"delta"`
	// Significant is whether the verdict cleared the significance bar (the practical
	// floor / significance gate the aggregator applied).
	Significant bool `json:"significant"`
	// Reason is a short human-readable explanation.
	Reason string `json:"reason,omitempty"`
}

// armView is one arm's rolling aggregate in the dossier.
type armView struct {
	// Name is the arm name (experimental / control / ...).
	Name string `json:"name"`
	// Count is the number of fitness samples folded into this arm.
	Count int `json:"count"`
	// MeanFitness is the running mean fitness for the arm.
	MeanFitness float64 `json:"mean_fitness"`
	// Variance is the population variance of the arm's fitness.
	Variance float64 `json:"variance"`
}

// gameView is one shadow game the experiment ran, reconstructed from the
// append-only event log.
type gameView struct {
	// GameID is the coordinator game id.
	GameID string `json:"game_id"`
	// Arm is the arm the game was bound to.
	Arm string `json:"arm"`
	// PairIndex is the Common-Random-Numbers pair this game belonged to, or nil if
	// the game was not part of a paired spawn.
	PairIndex *int `json:"pair_index,omitempty"`
	// Outcome is "concluded" (a usable fitness sample was folded), "released" (the
	// game ended without a usable sample), or "assigned" (bound but no terminal
	// event seen yet).
	Outcome string `json:"outcome"`
	// Fitness is the game's fitness sample (concluded games only).
	Fitness *float64 `json:"fitness,omitempty"`
	// DurationS is the game's wall-clock duration in seconds (concluded games).
	DurationS float64 `json:"duration_s,omitempty"`
	// Note is any audit annotation carried on the game's terminal event (e.g. the
	// release reason).
	Note string `json:"note,omitempty"`
	// TraceHint is a best-effort deep-link hint to the game's trace in Jaeger. The
	// journal does not (yet) persist trace ids, so this is the game_id callers can
	// search by — see traceHints on the dossier for the documented limitation.
	TraceHint string `json:"trace_hint,omitempty"`
}

// dossierView is the full GET /experiments/{id} payload.
type dossierView struct {
	// ID is the experiment id.
	ID string `json:"id"`
	// Thesis is the experiment's immutable intent — the idea / candidate / flag /
	// objective being tested.
	Thesis thesisView `json:"thesis"`
	// Status is the lifecycle status.
	Status string `json:"status"`
	// StartedAt / ConcludedAt bracket the experiment.
	StartedAt   time.Time  `json:"started_at"`
	ConcludedAt *time.Time `json:"concluded_at,omitempty"`
	// Arms are the per-arm rolling aggregates.
	Arms []armView `json:"arms"`
	// Games are the shadow games run, in event order, with per-game fitness + outcome.
	Games []gameView `json:"games"`
	// Verdict is the current/terminal verdict.
	Verdict *verdictView `json:"verdict,omitempty"`
	// Decision is the terminal decision label derived from the outcome verdict:
	// "promoted" / "discarded" / "refined", or "" while still running.
	Decision string `json:"decision,omitempty"`
	// Notes is the ordered list of decision/outcome audit notes (the closest the
	// journal carries to the retro's narrative; see the dossier doc on retro
	// suggestions).
	Notes []noteView `json:"notes,omitempty"`
	// TraceHints documents how to find this experiment's traces. The journal does
	// not persist trace ids today, so these are search hints, not URLs.
	TraceHints traceHints `json:"trace_hints"`
	// EventCount is the total number of events in the audit log.
	EventCount int `json:"event_count"`
}

// thesisView is the immutable intent surfaced as the experiment's thesis.
type thesisView struct {
	Hypothesis    string   `json:"hypothesis"`
	FlagKey       string   `json:"flag_key"`
	Candidate     any      `json:"candidate"`
	Objective     string   `json:"objective"`
	TargetNPerArm int      `json:"target_n_per_arm"`
	Arms          []string `json:"arms"`
}

// noteView is a decision/outcome audit annotation with its timestamp.
type noteView struct {
	At     time.Time `json:"at"`
	Kind   string    `json:"kind"`
	Status string    `json:"status,omitempty"`
	Note   string    `json:"note,omitempty"`
}

// traceHints tells a human how to locate the experiment's traces. The journal is
// trace-id-free today (#1181 part A wires the experiment-as-root span hierarchy
// separately); until those ids are persisted on journal events the dossier offers
// search hints keyed by the ids it DOES carry.
type traceHints struct {
	// ExperimentID is the experiment id to search for in the trace store.
	ExperimentID string `json:"experiment_id"`
	// GameIDs are the shadow game ids whose game spans belong to this experiment.
	GameIDs []string `json:"game_ids,omitempty"`
	// Note explains the current limitation.
	Note string `json:"note"`
}

// --- reader: disk → view ---

// list reads every experiment journal under root and projects each to a list
// item, newest-created first. A missing root yields an empty list (a first-run
// agent has no experiments), never an error.
func (d *dossierReader) list() ([]experimentListItem, error) {
	ids, err := journal.List(d.root)
	if err != nil {
		return nil, err
	}
	items := make([]experimentListItem, 0, len(ids))
	for _, id := range ids {
		j, err := journal.Load(d.root, id)
		if err != nil {
			// A single unreadable journal must not sink the whole list — skip it.
			continue
		}
		intent := j.Intent()
		summary := j.Summary()
		item := experimentListItem{
			ID:         intent.ExperimentID,
			Hypothesis: intent.Hypothesis,
			FlagKey:    intent.FlagKey,
			Candidate:  intent.ExperimentalValue,
			Objective:  intent.Objective,
			Status:     summary.Status,
			StartedAt:  intent.CreatedAt,
			Verdict:    toVerdictView(summary.Verdict),
		}
		// concludedAt requires the event log; only read it for terminal experiments.
		if isTerminalStatus(summary.Status) {
			if events, err := j.Events(); err == nil {
				item.ConcludedAt = concludedAt(events)
			}
		}
		items = append(items, item)
	}
	sort.SliceStable(items, func(a, b int) bool {
		return items[a].StartedAt.After(items[b].StartedAt)
	})
	return items, nil
}

// errDossierNotFound is returned by dossier when no journal exists for the id.
var errDossierNotFound = errors.New("experiment not found")

// dossier loads one experiment's full journal and projects it to the dossier
// view. It returns errDossierNotFound (→ 404) when the id has no intent.json on
// disk, and any other error (→ 500) when the journal is present but unreadable.
func (d *dossierReader) dossier(id string) (*dossierView, error) {
	j, err := journal.Load(d.root, id)
	if err != nil {
		// journal.Load fails with a "read intent" error when intent.json is absent —
		// treat a missing-file load as not-found, everything else as a read error.
		if errors.Is(err, os.ErrNotExist) || strings.Contains(err.Error(), "load intent") {
			return nil, errDossierNotFound
		}
		return nil, err
	}
	intent := j.Intent()
	summary := j.Summary()
	events, err := j.Events()
	if err != nil {
		return nil, err
	}

	view := &dossierView{
		ID: intent.ExperimentID,
		Thesis: thesisView{
			Hypothesis:    intent.Hypothesis,
			FlagKey:       intent.FlagKey,
			Candidate:     intent.ExperimentalValue,
			Objective:     intent.Objective,
			TargetNPerArm: intent.TargetNPerArm,
			Arms:          intent.Arms,
		},
		Status:     summary.Status,
		StartedAt:  intent.CreatedAt,
		Arms:       toArmViews(summary.Arms),
		Games:      gamesFromEvents(events),
		Verdict:    toVerdictView(summary.Verdict),
		Notes:      notesFromEvents(events),
		EventCount: summary.EventCount,
	}
	view.Decision = decisionLabel(summary.Verdict, summary.Status)
	view.ConcludedAt = concludedAt(events)
	view.TraceHints = traceHints{
		ExperimentID: intent.ExperimentID,
		GameIDs:      gameIDs(view.Games),
		Note:         "the journal does not persist trace ids yet; search the trace store by experiment_id / game_id (#1181 part A wires the experiment-as-root span hierarchy)",
	}
	return view, nil
}

// --- projection helpers ---

func toVerdictView(v *journal.Verdict) *verdictView {
	if v == nil {
		return nil
	}
	return &verdictView{
		Outcome:     v.Outcome,
		Delta:       v.Delta,
		Significant: v.Significant,
		Reason:      v.Reason,
	}
}

// toArmViews projects the per-arm Welford state to sorted arm views (deterministic
// order so the JSON is stable across reads).
func toArmViews(arms map[string]*journal.ArmStat) []armView {
	out := make([]armView, 0, len(arms))
	for name, stat := range arms {
		if stat == nil {
			continue
		}
		out = append(out, armView{
			Name:        name,
			Count:       stat.Count,
			MeanFitness: stat.Mean,
			Variance:    stat.Variance(),
		})
	}
	sort.Slice(out, func(a, b int) bool { return out[a].Name < out[b].Name })
	return out
}

// gamesFromEvents reconstructs the per-game timeline from the append-only log: a
// game_assigned event introduces a game (and its arm/pair binding), and a later
// game_concluded / game_released event finalizes its outcome + fitness. Games are
// returned in first-seen (assignment) order.
func gamesFromEvents(events []journal.Event) []gameView {
	idx := make(map[string]int)
	var games []gameView
	for _, e := range events {
		if e.GameID == "" {
			continue
		}
		i, seen := idx[e.GameID]
		if !seen {
			i = len(games)
			idx[e.GameID] = i
			games = append(games, gameView{GameID: e.GameID, Outcome: "assigned", TraceHint: e.GameID})
		}
		g := &games[i]
		if e.Arm != "" {
			g.Arm = e.Arm
		}
		if e.PairIndex != nil {
			pi := *e.PairIndex
			g.PairIndex = &pi
		}
		switch e.Kind {
		case journal.KindGameConcluded:
			g.Outcome = "concluded"
			if e.Fitness != nil {
				f := *e.Fitness
				g.Fitness = &f
			}
			g.DurationS = e.DurationS
			if e.Note != "" {
				g.Note = e.Note
			}
		case journal.KindGameReleased:
			g.Outcome = "released"
			if e.Note != "" {
				g.Note = e.Note
			}
		}
	}
	return games
}

// notesFromEvents collects the decision/outcome audit annotations in order — the
// closest the journal carries to a narrative of the experiment's progression.
// KindInterimVerdict is DELIBERATELY excluded: an interim verdict is a rolling
// snapshot, not a decision/conclusion, and its current value is already surfaced via
// the summary's Verdict — including it here would duplicate noise per conclusion.
func notesFromEvents(events []journal.Event) []noteView {
	var notes []noteView
	for _, e := range events {
		if e.Kind != journal.KindDecision && e.Kind != journal.KindOutcome {
			continue
		}
		if e.Note == "" && e.Status == "" {
			continue
		}
		notes = append(notes, noteView{At: e.At, Kind: string(e.Kind), Status: e.Status, Note: e.Note})
	}
	return notes
}

// concludedAt returns the timestamp of the first terminal outcome event, or nil if
// the experiment has not concluded.
func concludedAt(events []journal.Event) *time.Time {
	for _, e := range events {
		if e.Kind == journal.KindOutcome {
			at := e.At
			return &at
		}
	}
	return nil
}

// gameIDs extracts the game ids from the reconstructed games (for the trace hints).
func gameIDs(games []gameView) []string {
	ids := make([]string, 0, len(games))
	for _, g := range games {
		ids = append(ids, g.GameID)
	}
	return ids
}

// decisionLabel maps the terminal verdict outcome to the human decision label the
// dossier reports. It returns "" while the experiment is still running (no terminal
// decision has been made). "inconclusive" at conclusion maps to "refined" — the
// experiment did not clear the bar and feeds the next thesis rather than promoting
// or discarding a candidate.
func decisionLabel(v *journal.Verdict, status string) string {
	if !isTerminalStatus(status) || v == nil {
		return ""
	}
	switch v.Outcome {
	case "promote":
		return "promoted"
	case "discard":
		return "discarded"
	default:
		return "refined"
	}
}

// isTerminalStatus reports whether a journal status string is a terminal lifecycle
// state. "running" / "proposed" are live; anything else (concluded / promoting /
// aborted / ...) is treated as terminal for the dossier's concluded-at + decision
// derivation.
func isTerminalStatus(status string) bool {
	switch status {
	case "", "running", "proposed":
		return false
	default:
		return true
	}
}

// --- HTTP handlers ---

// registerDossierRoutes mounts the read-only dossier endpoints on mux. It is wired
// onto the agent's existing health HTTP server (server.go) so the dossier shares
// the agent's lifecycle — no extra port, no extra server to start/stop. All routes
// are GET-only and read the journal off disk, so they never block the agent loops.
func registerDossierRoutes(mux *http.ServeMux, reader *dossierReader) {
	mux.HandleFunc("/experiments", reader.handleList)
	mux.HandleFunc("/experiments/", reader.handleDossier)
}

// handleList serves GET /experiments. It returns JSON by default and a minimal
// HTML index when the request prefers HTML (Accept header) or carries ?format=html.
func (d *dossierReader) handleList(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	items, err := d.list()
	if err != nil {
		writeJSONError(w, http.StatusInternalServerError, "failed to read experiments")
		return
	}
	if wantsHTML(r) {
		writeListHTML(w, items)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"experiments": items})
}

// handleDossier serves GET /experiments/{id}. The id is the path segment after
// "/experiments/"; an empty id (a trailing-slash request) is a 404.
func (d *dossierReader) handleDossier(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
		return
	}
	id := strings.TrimPrefix(r.URL.Path, "/experiments/")
	id = strings.Trim(id, "/")
	if id == "" || strings.Contains(id, "/") {
		writeJSONError(w, http.StatusNotFound, "experiment not found")
		return
	}
	view, err := d.dossier(id)
	if err != nil {
		if errors.Is(err, errDossierNotFound) {
			writeJSONError(w, http.StatusNotFound, "experiment not found")
			return
		}
		writeJSONError(w, http.StatusInternalServerError, "failed to read experiment")
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// wantsHTML reports whether the client prefers an HTML response — either via an
// explicit ?format=html query or an Accept header that favors HTML over JSON.
func wantsHTML(r *http.Request) bool {
	if r.URL.Query().Get("format") == "html" {
		return true
	}
	accept := r.Header.Get("Accept")
	return strings.Contains(accept, "text/html") && !strings.Contains(accept, "application/json")
}

// writeJSON encodes v as indented JSON with the given status.
func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

// writeJSONError writes a small {"error": msg} body with the given status.
func writeJSONError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// writeListHTML renders the minimal human-readable experiment index. It is a plain
// table with a link to each experiment's JSON dossier — deliberately tiny (no
// templates, no CSS framework); the JSON is the real contract.
func writeListHTML(w http.ResponseWriter, items []experimentListItem) {
	var b strings.Builder
	b.WriteString("<!doctype html><html><head><meta charset=\"utf-8\">")
	b.WriteString("<title>Experiment dossiers</title></head><body>")
	b.WriteString("<h1>Experiment dossiers</h1>")
	if len(items) == 0 {
		b.WriteString("<p>No experiments yet.</p></body></html>")
	} else {
		b.WriteString("<table border=\"1\" cellpadding=\"4\"><tr>")
		b.WriteString("<th>ID</th><th>Status</th><th>Flag</th><th>Candidate</th>")
		b.WriteString("<th>Verdict</th><th>Started</th><th>Thesis</th></tr>")
		for _, it := range items {
			verdict := "—"
			if it.Verdict != nil {
				verdict = it.Verdict.Outcome
			}
			b.WriteString("<tr><td><a href=\"/experiments/")
			b.WriteString(html.EscapeString(it.ID))
			b.WriteString("\">")
			b.WriteString(html.EscapeString(it.ID))
			b.WriteString("</a></td><td>")
			b.WriteString(html.EscapeString(it.Status))
			b.WriteString("</td><td>")
			b.WriteString(html.EscapeString(it.FlagKey))
			b.WriteString("</td><td>")
			b.WriteString(html.EscapeString(candidateString(it.Candidate)))
			b.WriteString("</td><td>")
			b.WriteString(html.EscapeString(verdict))
			b.WriteString("</td><td>")
			b.WriteString(html.EscapeString(it.StartedAt.Format(time.RFC3339)))
			b.WriteString("</td><td>")
			b.WriteString(html.EscapeString(it.Hypothesis))
			b.WriteString("</td></tr>")
		}
		b.WriteString("</table></body></html>")
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(b.String()))
}

// candidateString renders an experimental-value (any JSON scalar/object) as a
// compact string for the HTML table cell.
func candidateString(v any) string {
	if v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	data, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	return string(data)
}
