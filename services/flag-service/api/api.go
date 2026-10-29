// Package api is the flag service's HTTP/JSON API (services.md §7.2, FR-134,
// FR-135, FR-139): one resource-style endpoint per flag set,
// /{flagSetId}/{flagName}, the game lifecycle under /games and /coordinators,
// and the read endpoints. There are no roles or tokens (architecture note
// D11): writer, decision_id and reason are informational and go on the span.
package api

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/trace"

	"github.com/joustmania/flag-service/fserr"
	"github.com/joustmania/flag-service/state"
	"github.com/joustmania/flag-service/syncserver"
	"github.com/joustmania/flag-service/telemetry"
)

// MaxBodyBytes bounds a request body.
const MaxBodyBytes = 64 << 10

// HeaderPersisted is set on the 204 of a DELETE ?persist=true that cleared a
// value: "true" when the state file was written, "false" when a kill switch was
// cleared in memory only because the save failed, or its clear could not reach
// a state file unreadable at start (#2368, services.md §7.2).
const HeaderPersisted = "X-Flag-Persisted"

// API serves the HTTP/JSON endpoints.
type API struct {
	State   *state.State
	Metrics *telemetry.Metrics
	Tracer  trace.Tracer
	Links   *syncserver.Links
	Logger  *slog.Logger
	// Ready reports whether the gRPC sync listener is up (GET /healthz).
	Ready func() bool
	Now   func() time.Time
}

func (a *API) now() time.Time {
	if a.Now != nil {
		return a.Now()
	}
	return time.Now()
}

// Handler returns the routed handler, instrumented with otelhttp so a write
// sits under the caller's span (W3C propagation).
func (a *API) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", a.healthz)
	mux.HandleFunc("GET /{$}", a.listSets)
	mux.HandleFunc("GET /state", a.stateView)
	mux.HandleFunc("GET /games", a.listGames)
	mux.HandleFunc("PUT /games/{gameId}", a.registerGame)
	mux.HandleFunc("DELETE /games/{gameId}", a.endGame)
	mux.HandleFunc("PUT /coordinators/{instance}/games", a.reconcile)
	mux.HandleFunc("GET /{set}", a.getSet)
	mux.HandleFunc("DELETE /{set}", a.clearWriter)
	mux.HandleFunc("GET /{set}/{flag}", a.getFlag)
	mux.HandleFunc("PUT /{set}/{flag}", a.put)
	mux.HandleFunc("POST /{set}/{flag}", a.post)
	mux.HandleFunc("DELETE /{set}/{flag}", a.clear)
	return otelhttp.NewHandler(mux, "flag-service",
		otelhttp.WithFilter(func(r *http.Request) bool { return r.URL.Path != "/healthz" }),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string {
			if r.Pattern != "" {
				return r.Pattern
			}
			return r.Method + " " + r.URL.Path
		}),
	)
}

// ---------------------------------------------------------------- responses

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err *fserr.Error) {
	writeJSON(w, err.Status, map[string]string{"error": err.Reason, "detail": err.Detail})
}

// decode reads a JSON body strictly (unknown fields are malformed).
func decode(r *http.Request, w http.ResponseWriter, into any) *fserr.Error {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, MaxBodyBytes))
	if err != nil {
		return fserr.BadRequest("read body: %v", err)
	}
	if len(bytes.TrimSpace(body)) == 0 {
		return fserr.BadRequest("empty body")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	dec.UseNumber()
	if err := dec.Decode(into); err != nil {
		return fserr.BadRequest("malformed JSON: %v", err)
	}
	if dec.More() {
		return fserr.BadRequest("trailing data after the JSON body")
	}
	return nil
}

// ---------------------------------------------------------------- reads

func (a *API) healthz(w http.ResponseWriter, _ *http.Request) {
	if a.Ready != nil && !a.Ready() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]any{"status": "starting", "epoch": a.State.Epoch()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"status": "ok", "epoch": a.State.Epoch()})
}

func (a *API) listSets(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"epoch": a.State.Epoch(), "flag_sets": a.State.Sets()})
}

func (a *API) stateView(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, a.State.Stats())
}

func (a *API) listGames(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"games": a.State.Games()})
}

func (a *API) getSet(w http.ResponseWriter, r *http.Request) {
	view, err := a.State.SetView(r.PathValue("set"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func (a *API) getFlag(w http.ResponseWriter, r *http.Request) {
	view, err := a.State.FlagView(r.PathValue("set"), r.PathValue("flag"))
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// ---------------------------------------------------------------- writes

type putBody struct {
	Variant    string          `json:"variant"`
	Value      json.RawMessage `json:"value"`
	Scope      state.Scope     `json:"scope"`
	TTLSeconds *float64        `json:"ttl_seconds"`
	Persist    bool            `json:"persist"`
	Writer     string          `json:"writer"`
	DecisionID string          `json:"decision_id"`
	Reason     string          `json:"reason"`
}

type postBody struct {
	Scope      state.Scope    `json:"scope"`
	Payload    map[string]any `json:"payload"`
	Writer     string         `json:"writer"`
	DecisionID string         `json:"decision_id"`
	Reason     string         `json:"reason"`
}

// finishWrite ends a flagservice.write span and records the write metrics.
func (a *API) finishWrite(ctx context.Context, span trace.Span, op string, req state.WriteRequest, res state.WriteResult, ferr *fserr.Error, start time.Time) {
	result, reason := telemetry.ResultOK, fserr.ReasonOK
	if ferr != nil {
		result, reason = telemetry.ResultRejected, ferr.Reason
		if ferr.Status >= 500 && ferr.Reason != fserr.NoSyncStream {
			result = telemetry.ResultError
		}
		span.SetStatus(codes.Error, ferr.Detail)
	}
	span.SetAttributes(telemetry.WriteAttrs(op, req, res, result, reason)...)
	span.End()
	if a.Metrics != nil {
		set, flag := a.State.MetricLabels(req.Set, req.Flag)
		a.Metrics.RecordWrite(ctx, set, flag, op, result, reason)
		a.Metrics.WriteDuration.Record(ctx, a.now().Sub(start).Seconds(), metric.WithAttributes(attribute.String("op", op)))
	}
	// A no-op clear changed nothing, so it must not take over the trace link
	// of the snapshot version a real write produced.
	noop := (op == telemetry.OpClear && res.Cleared == 0) || op == telemetry.OpDryRun
	if ferr == nil && !noop && a.Links != nil && res.SnapshotVersion > 0 {
		a.Links.Remember(res.SnapshotVersion, span.SpanContext(), start)
	}
}

func (a *API) put(w http.ResponseWriter, r *http.Request) {
	start := a.now()
	ctx, span := a.Tracer.Start(r.Context(), telemetry.SpanWrite)
	req := state.WriteRequest{Set: r.PathValue("set"), Flag: r.PathValue("flag")}
	var body putBody
	ferr := decode(r, w, &body)
	var res state.WriteResult
	if ferr == nil {
		req.Variant, req.Scope, req.TTLSeconds, req.Persist = body.Variant, body.Scope, body.TTLSeconds, body.Persist
		req.Writer, req.DecisionID = body.Writer, body.DecisionID
		req.DryRun, ferr = boolParam(r.URL.Query(), "dry_run")
		if v := bytes.TrimSpace(body.Value); len(v) > 0 && !bytes.Equal(v, []byte("null")) {
			req.Value = v
		}
		if body.Reason != "" {
			span.SetAttributes(attribute.String("flagservice.write_reason", body.Reason))
		}
		if ferr == nil {
			req.IfWriterIn, ferr = ifWriterIn(r)
		}
		if ferr == nil {
			res, ferr = a.State.Put(req)
		} else {
			res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch(), ScopeKey: req.Scope.Name()}
		}
	} else {
		res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch()}
	}
	op := telemetry.OpSet
	if req.DryRun {
		op = telemetry.OpDryRun
	}
	a.finishWrite(ctx, span, op, req, res, ferr, start)
	if ferr != nil {
		writeError(w, ferr)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// ifWriterIn parses ?if_writer_in=<label>,<label>,... (#2260, flags.md §13):
// the PUT write precondition. nil = absent. An empty entry means "no real
// overlay present", so "promotion," is the promotion's own overlay or none.
// Strict: a repeated parameter is malformed, it must not silently pick one.
func ifWriterIn(r *http.Request) ([]string, *fserr.Error) {
	vals, ok := r.URL.Query()["if_writer_in"]
	if !ok {
		return nil, nil
	}
	if len(vals) != 1 {
		return nil, fserr.BadRequest("if_writer_in must be given once")
	}
	return strings.Split(vals[0], ","), nil
}

func (a *API) post(w http.ResponseWriter, r *http.Request) {
	start := a.now()
	ctx, span := a.Tracer.Start(r.Context(), telemetry.SpanWrite)
	req := state.WriteRequest{Set: r.PathValue("set"), Flag: r.PathValue("flag")}
	var body postBody
	ferr := decode(r, w, &body)
	var res state.WriteResult
	if ferr == nil {
		req.Scope, req.Writer, req.DecisionID = body.Scope, body.Writer, body.DecisionID
		if body.Reason != "" {
			span.SetAttributes(attribute.String("flagservice.write_reason", body.Reason))
		}
		res, ferr = a.State.Append(state.AppendRequest{
			Set: req.Set, Flag: req.Flag, Scope: body.Scope, Payload: body.Payload,
			Writer: body.Writer, DecisionID: body.DecisionID,
		})
	} else {
		res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch()}
	}
	a.finishWrite(ctx, span, telemetry.OpAppend, req, res, ferr, start)
	if ferr != nil {
		writeError(w, ferr)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// boolParam parses one boolean query parameter, strictly (#2367, services.md
// flag-service API): true|false|1|0, absent = false. Anything else (yes, True,
// empty, a repeated parameter) is a 400 naming the parameter: a loose parse
// would turn a dry run into a real write or pick the wrong scope or layer.
func boolParam(q url.Values, name string) (bool, *fserr.Error) {
	vals, ok := q[name]
	if !ok {
		return false, nil
	}
	if len(vals) == 1 {
		switch vals[0] {
		case "true", "1":
			return true, nil
		case "false", "0":
			return false, nil
		}
	}
	return false, fserr.BadRequest("%s must be true, false, 1 or 0", name)
}

func scopeFromQuery(r *http.Request) (state.Scope, *fserr.Error) {
	q := r.URL.Query()
	sc := state.Scope{
		GameID: q.Get("game_id"), Serial: q.Get("serial"),
		ExperimentID: q.Get("experiment_id"), Arm: q.Get("arm"), GameKind: q.Get("game_kind"),
	}
	fraction, ferr := boolParam(q, "fraction")
	if fraction || q.Has("fraction_percent") {
		zero := 0
		sc.FractionPercent = &zero
	}
	return sc, ferr
}

func (a *API) clear(w http.ResponseWriter, r *http.Request) {
	start := a.now()
	ctx, span := a.Tracer.Start(r.Context(), telemetry.SpanWrite)
	sc, ferr := scopeFromQuery(r)
	var persist, guarded bool
	if ferr == nil {
		persist, ferr = boolParam(r.URL.Query(), "persist")
	}
	req := state.WriteRequest{Set: r.PathValue("set"), Flag: r.PathValue("flag"), Scope: sc, Writer: r.URL.Query().Get("writer")}
	// ?unless_real_game=true: the agent's guarded revert (#2236, flags.md §13).
	// Strict parse: a typo must not silently turn the guard off.
	if ferr == nil {
		guarded, ferr = boolParam(r.URL.Query(), "unless_real_game")
	}
	var res state.WriteResult
	if ferr != nil {
		res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch(), ScopeKey: sc.Name()}
	}
	// ?if_writer=<label>: the compare-and-delete precondition (#1877, flags.md
	// §13). Strict: an empty value must not silently drop the precondition.
	ifWriter := r.URL.Query().Get("if_writer")
	if ferr == nil && r.URL.Query().Has("if_writer") && ifWriter == "" {
		ferr = fserr.BadRequest("if_writer must not be empty")
		res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch(), ScopeKey: sc.Name()}
	}
	if ferr == nil && len(ifWriter) > state.MaxIDLength {
		ferr = fserr.BadRequest("if_writer is longer than %d characters", state.MaxIDLength)
		res = state.WriteResult{FlagSet: req.Set, Flag: req.Flag, Epoch: a.State.Epoch(), ScopeKey: sc.Name()}
	}
	if ferr == nil {
		res, ferr = a.State.Clear(state.ClearRequest{Set: req.Set, Flag: req.Flag, Scope: sc, Writer: req.Writer, Persist: persist, UnlessRealGame: guarded, IfWriter: ifWriter})
	}
	span.SetAttributes(attribute.Int("flagservice.cleared", res.Cleared))
	a.finishWrite(ctx, span, telemetry.OpClear, req, res, ferr, start)
	if ferr != nil {
		writeError(w, ferr)
		return
	}
	if persist && (res.Cleared > 0 || res.PersistFailed) {
		// #2368: a persisted clear says whether it reached the state file; a
		// kill switch cleared in memory only, or whose clear could not reach
		// an unreadable state file, answers "false".
		w.Header().Set(HeaderPersisted, strconv.FormatBool(!res.PersistFailed))
	}
	w.WriteHeader(http.StatusNoContent)
}

func (a *API) clearWriter(w http.ResponseWriter, r *http.Request) {
	start := a.now()
	ctx, span := a.Tracer.Start(r.Context(), telemetry.SpanWrite)
	req := state.WriteRequest{Set: r.PathValue("set"), Writer: r.URL.Query().Get("writer")}
	n, ferr := a.State.ClearWriter(req.Set, req.Writer)
	span.SetAttributes(attribute.Int("flagservice.cleared", n))
	res := state.WriteResult{FlagSet: req.Set, Epoch: a.State.Epoch(), ScopeKey: "stack", Cleared: n}
	if ferr == nil {
		res.SnapshotVersion = a.State.Version()
	}
	a.finishWrite(ctx, span, telemetry.OpClear, req, res, ferr, start)
	if ferr != nil {
		writeError(w, ferr)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

// ---------------------------------------------------------------- lifecycle
// The lifecycle calls change no flag value flagd must see first, so they are
// exempt from 503 no_sync_stream (re-review N6).

type gameBody struct {
	CoordinatorInstance string   `json:"coordinator_instance"`
	GameKind            string   `json:"game_kind"`
	Serials             []string `json:"serials"`
	ExperimentID        string   `json:"experiment_id"`
	Arm                 string   `json:"arm"`
	// GameID is accepted (and must match) in the reconcile body.
	GameID string `json:"game_id"`
}

func (b gameBody) game(id string) state.Game {
	return state.Game{
		ID: id, CoordinatorInstance: b.CoordinatorInstance, Kind: b.GameKind,
		Serials: b.Serials, ExperimentID: b.ExperimentID, Arm: b.Arm,
	}
}

func (a *API) registerGame(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("gameId")
	_, span := a.Tracer.Start(r.Context(), telemetry.SpanGameRegister, trace.WithAttributes(attribute.String("game.id", id)))
	defer span.End()
	var body gameBody
	if ferr := decode(r, w, &body); ferr != nil {
		span.SetAttributes(attribute.String("flagservice.result", telemetry.ResultRejected), attribute.String("flagservice.reason", ferr.Reason))
		writeError(w, ferr)
		return
	}
	if body.GameID != "" && body.GameID != id {
		ferr := fserr.BadRequest("body game_id %q does not match the path %q", body.GameID, id)
		span.SetAttributes(attribute.String("flagservice.result", telemetry.ResultRejected), attribute.String("flagservice.reason", ferr.Reason))
		writeError(w, ferr)
		return
	}
	g := body.game(id)
	span.SetAttributes(
		attribute.String("game.kind", g.Kind),
		attribute.Int("player.count", len(g.Serials)),
		attribute.String("experiment.id", g.ExperimentID),
		attribute.String("experiment.arm", g.Arm),
		attribute.String("flagservice.coordinator_instance", g.CoordinatorInstance),
	)
	already, ferr := a.State.RegisterGame(g)
	if ferr != nil {
		span.SetAttributes(attribute.String("flagservice.result", telemetry.ResultRejected), attribute.String("flagservice.reason", ferr.Reason))
		writeError(w, ferr)
		return
	}
	statusText := "registered"
	if already {
		statusText = fserr.ReasonRegistered
	}
	span.SetAttributes(attribute.String("flagservice.result", telemetry.ResultOK), attribute.String("flagservice.reason", statusText))
	writeJSON(w, http.StatusOK, map[string]any{"game_id": id, "status": statusText, "epoch": a.State.Epoch()})
}

func (a *API) endGame(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("gameId")
	end, found := a.State.EndGame(id, state.EndReasonEnded)
	if found {
		telemetry.GameEndSpan(r.Context(), a.Tracer, end)
	}
	w.WriteHeader(http.StatusNoContent)
}

type reconcileBody struct {
	Games []gameBody `json:"games"`
}

func (a *API) reconcile(w http.ResponseWriter, r *http.Request) {
	instance := r.PathValue("instance")
	var body reconcileBody
	if ferr := decode(r, w, &body); ferr != nil {
		writeError(w, ferr)
		return
	}
	games := make([]state.Game, 0, len(body.Games))
	for _, gb := range body.Games {
		if gb.GameID == "" {
			writeError(w, fserr.BadRequest("every reconciled game needs game_id"))
			return
		}
		games = append(games, gb.game(gb.GameID))
	}
	ends, ferr := a.State.Reconcile(instance, games)
	if ferr != nil {
		writeError(w, ferr)
		return
	}
	dropped := make([]string, 0, len(ends))
	for _, end := range ends {
		telemetry.GameEndSpan(r.Context(), a.Tracer, end)
		dropped = append(dropped, end.GameID)
	}
	writeJSON(w, http.StatusOK, map[string]any{"coordinator_instance": instance, "games": len(games), "dropped": dropped, "epoch": a.State.Epoch()})
}
