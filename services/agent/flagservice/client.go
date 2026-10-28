// Package flagservice is the agent's client of the flag service's HTTP/JSON API
// (epic #1407, docs/specs/services.md §7.2). The agent changes runtime flags
// ONLY through it: no component writes flag files (C-032, C-033).
//
// Every call has a 500 ms deadline and fails closed: a write that did not get a
// 2xx is reported as an error and never retried for the same decision, except a
// game-scoped write rejected with 409 game_not_live, which is retried once after
// 100 ms to cover a game registration still in flight (UC-053 A6).
package flagservice

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"
)

// Defaults (services.md §7.2).
const (
	DefaultURL         = "http://flag-service:8070"
	RequestTimeout     = 500 * time.Millisecond
	GameNotLiveBackoff = 100 * time.Millisecond
)

// Reason codes the agent acts on.
const (
	ReasonGameNotLive  = "game_not_live"
	ReasonNoSyncStream = "no_sync_stream"
	// ReasonRealGameLive refuses a real write of a hold key while a real game is
	// registered (409, flags.md §13, UC-043 A17); the promoter defers the write.
	ReasonRealGameLive = "real_game_live"
	// ReasonWriterMismatch refuses a compare-and-delete (DELETE ?if_writer=,
	// 412, #1877) or a guarded write (PUT ?if_writer_in=, 412, #2260): the
	// overlay belongs to another writer; nothing was cleared or written.
	ReasonWriterMismatch = "writer_mismatch"
)

// Scope is a write's scope properties.
type Scope struct {
	GameID          string `json:"game_id,omitempty"`
	Serial          string `json:"serial,omitempty"`
	ExperimentID    string `json:"experiment_id,omitempty"`
	Arm             string `json:"arm,omitempty"`
	GameKind        string `json:"game_kind,omitempty"`
	FractionPercent *int   `json:"fraction_percent,omitempty"`
}

// Write is a PUT body: exactly one of Variant and Value.
type Write struct {
	Variant    string   `json:"variant,omitempty"`
	Value      any      `json:"value,omitempty"`
	Scope      Scope    `json:"scope,omitempty"`
	TTLSeconds *float64 `json:"ttl_seconds,omitempty"`
	Persist    bool     `json:"persist,omitempty"`
	Writer     string   `json:"writer,omitempty"`
	DecisionID string   `json:"decision_id,omitempty"`
	Reason     string   `json:"reason,omitempty"`
	// DryRun sends PUT ?dry_run=true: the service validates and renders the
	// write, answers the flag before and after, and applies nothing.
	DryRun bool `json:"-"`
	// IfWriterIn sends PUT ?if_writer_in=<labels joined by ","> (#2260, UC-043
	// A19): the real overlay is written only when its stored writer is in the
	// list, or none is present and the list holds "". nil = no precondition.
	IfWriterIn []string `json:"-"`
}

// Event is a POST body (one event appended to a game's event list).
type Event struct {
	Scope      Scope          `json:"scope"`
	Payload    map[string]any `json:"payload"`
	Writer     string         `json:"writer,omitempty"`
	DecisionID string         `json:"decision_id,omitempty"`
	Reason     string         `json:"reason,omitempty"`
}

// Result is the service's WriteResult.
type Result struct {
	FlagSet         string `json:"flag_set"`
	Flag            string `json:"flag"`
	Variant         string `json:"variant"`
	Seq             int64  `json:"seq"`
	Epoch           string `json:"epoch"`
	SnapshotVersion uint64 `json:"snapshot_version"`
	Persisted       bool   `json:"persisted"`
	// Dry runs only: the rendered flagd flag before and after the write.
	DryRun         bool            `json:"dry_run,omitempty"`
	RenderedBefore json.RawMessage `json:"rendered_before,omitempty"`
	Rendered       json.RawMessage `json:"rendered,omitempty"`
}

// Error is a rejected request (non-2xx) with the service's reason code.
type Error struct {
	Status int
	Reason string
	Detail string
}

func (e *Error) Error() string {
	return fmt.Sprintf("flag service: HTTP %d %s: %s", e.Status, e.Reason, e.Detail)
}

// IsReason reports whether err is a rejection with the given reason code.
func IsReason(err error, reason string) bool {
	var e *Error
	return errors.As(err, &e) && e.Reason == reason
}

// Client talks to one flag service.
type Client struct {
	base    string
	http    *http.Client
	timeout time.Duration
	backoff time.Duration
}

// New builds a client for base (e.g. http://flag-service:8070).
func New(base string) *Client {
	return &Client{
		base:    strings.TrimRight(base, "/"),
		http:    &http.Client{},
		timeout: RequestTimeout,
		backoff: GameNotLiveBackoff,
	}
}

// NewFromEnv builds a client for FLAG_SERVICE_URL (default DefaultURL).
func NewFromEnv() *Client {
	base := os.Getenv("FLAG_SERVICE_URL")
	if base == "" {
		base = DefaultURL
	}
	return New(base)
}

// WithTiming overrides the request deadline and the game_not_live backoff (tests).
func (c *Client) WithTiming(timeout, backoff time.Duration) *Client {
	c.timeout, c.backoff = timeout, backoff
	return c
}

// BaseURL returns the service URL.
func (c *Client) BaseURL() string { return c.base }

func flagPath(set, flag string) string {
	return "/" + url.PathEscape(set) + "/" + url.PathEscape(flag)
}

// do sends one request with the deadline, propagating the W3C trace context so
// the service's flagservice.write span sits under the caller's span.
func (c *Client) do(ctx context.Context, method, path string, body any, out any) error {
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	var rd io.Reader
	if body != nil {
		raw, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("flag service: encode %s %s: %w", method, path, err)
		}
		rd = bytes.NewReader(raw)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.base+path, rd)
	if err != nil {
		return fmt.Errorf("flag service: %w", err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("flag service: %s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode/100 != 2 {
		e := &Error{Status: resp.StatusCode}
		var body struct {
			Error  string `json:"error"`
			Detail string `json:"detail"`
		}
		if json.Unmarshal(raw, &body) == nil {
			e.Reason, e.Detail = body.Error, body.Detail
		}
		return e
	}
	if out != nil && len(raw) > 0 {
		if err := json.Unmarshal(raw, out); err != nil {
			return fmt.Errorf("flag service: decode %s %s: %w", method, path, err)
		}
	}
	return nil
}

// retryGameNotLive runs fn, and once more after the backoff when a game-scoped
// write was rejected with 409 game_not_live (registration still in flight).
func (c *Client) retryGameNotLive(ctx context.Context, gameScoped bool, fn func() error) error {
	err := fn()
	if err == nil || !gameScoped || !IsReason(err, ReasonGameNotLive) {
		return err
	}
	select {
	case <-ctx.Done():
		return err
	case <-time.After(c.backoff):
	}
	return fn()
}

// Put sets a value in a scope (PUT /{set}/{flag}).
func (c *Client) Put(ctx context.Context, set, flag string, w Write) (Result, error) {
	var res Result
	path := flagPath(set, flag)
	query := url.Values{}
	if w.DryRun {
		query.Set("dry_run", "true")
	}
	if w.IfWriterIn != nil {
		query.Set("if_writer_in", strings.Join(w.IfWriterIn, ","))
	}
	if len(query) > 0 {
		path += "?" + query.Encode()
	}
	err := c.retryGameNotLive(ctx, w.Scope.GameID != "", func() error {
		return c.do(ctx, http.MethodPut, path, w, &res)
	})
	return res, err
}

// Post appends an event to a game's event list (POST /{set}/{flag}).
func (c *Client) Post(ctx context.Context, set, flag string, ev Event) (Result, error) {
	if ev.Payload == nil {
		ev.Payload = map[string]any{}
	}
	var res Result
	err := c.retryGameNotLive(ctx, ev.Scope.GameID != "", func() error {
		return c.do(ctx, http.MethodPost, flagPath(set, flag), ev, &res)
	})
	return res, err
}

// Delete clears a value (DELETE /{set}/{flag}?<query>). query holds the scope
// properties (game_id, serial, experiment_id, arm, game_kind) and writer.
func (c *Client) Delete(ctx context.Context, set, flag string, query url.Values) error {
	path := flagPath(set, flag)
	if len(query) > 0 {
		path += "?" + query.Encode()
	}
	return c.do(ctx, http.MethodDelete, path, nil, nil)
}

// Policy is a flag's registry row as the read endpoints show it.
type Policy struct {
	Kind    string   `json:"kind"`
	Scopes  []string `json:"scope"`
	Persist bool     `json:"persist"`
}

// AllowsScope reports whether the registry allows writes in scope.
func (p *Policy) AllowsScope(scope string) bool {
	if p == nil {
		return false
	}
	for _, s := range p.Scopes {
		if s == scope {
			return true
		}
	}
	return false
}

// ValueView is one runtime value (an overlay or a scoped value).
type ValueView struct {
	Variant string          `json:"variant"`
	Value   json.RawMessage `json:"value"`
	Writer  string          `json:"writer"`
}

// FlagView is the part of GET /{set}[/{flag}] the agent reads.
type FlagView struct {
	Flag             string                     `json:"flag"`
	Mutable          bool                       `json:"mutable"`
	Policy           *Policy                    `json:"policy"`
	BaselineDefault  string                     `json:"baseline_default"`
	EffectiveDefault string                     `json:"effective_default"`
	Variants         []string                   `json:"variants"`
	VariantValues    map[string]json.RawMessage `json:"variant_values"`
	// StackOverlay is the stack-scoped overlay (operator, stage, agent), with its writer.
	StackOverlay *ValueView `json:"stack_overlay"`
	RealOverlay  *ValueView `json:"real_overlay"`
	// Rendered is the flag as flagd reads it (GET /{set}/{flag} only).
	Rendered json.RawMessage `json:"rendered"`
}

// SetView is GET /{set}.
type SetView struct {
	FlagSet string     `json:"flag_set"`
	Served  bool       `json:"served"`
	Flags   []FlagView `json:"flags"`
}

// Epoch reads the service's boot epoch (GET /healthz). A different epoch than
// the one a write returned means the service restarted and lost every
// in-memory value, including experiment-arm values.
func (c *Client) Epoch(ctx context.Context) (string, error) {
	var h struct {
		Epoch string `json:"epoch"`
	}
	if err := c.do(ctx, http.MethodGet, "/healthz", nil, &h); err != nil {
		return "", err
	}
	return h.Epoch, nil
}

// Game is one registered game scope (GET /games).
type Game struct {
	GameID   string `json:"game_id"`
	GameKind string `json:"game_kind"`
}

// ListGames reads the registered game scopes (GET /games): the flag service's
// authoritative live-game set (UC-043 A17).
func (c *Client) ListGames(ctx context.Context) ([]Game, error) {
	var v struct {
		Games []Game `json:"games"`
	}
	err := c.do(ctx, http.MethodGet, "/games", nil, &v)
	return v.Games, err
}

// GetFlag reads one flag's effective state with its rendered flagd JSON.
func (c *Client) GetFlag(ctx context.Context, set, flag string) (FlagView, error) {
	var v FlagView
	err := c.do(ctx, http.MethodGet, flagPath(set, flag), nil, &v)
	return v, err
}

// GetSet reads every flag of a set.
func (c *Client) GetSet(ctx context.Context, set string) (SetView, error) {
	var v SetView
	err := c.do(ctx, http.MethodGet, "/"+url.PathEscape(set), nil, &v)
	return v, err
}
