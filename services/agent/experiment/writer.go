package experiment

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"

	"github.com/joustmania/agent/flagservice"
)

// GameFlagSet is the only flag set an experiment writes. The Writer is
// HARDCODED to it: there is no constructor that takes another set, so the agent
// cannot write its own governance flags (OWNERSHIP_MODEL §1, C-008).
const GameFlagSet = "game"

// experimentWriter is the informational writer label on experiment writes (D11).
const experimentWriter = "agent-experiment"

// ErrUnscoped refuses a proposal without an experiment id: every experiment
// value is scoped to one experiment and its experimental arm (FR-138); the
// legacy "every shadow game" rule is gone (#1415).
var ErrUnscoped = errors.New("experiment proposal needs an experiment id (FR-138)")

// FlagService is the part of the flag-service client the Writer and the Gate use.
type FlagService interface {
	Put(ctx context.Context, set, flag string, w flagservice.Write) (flagservice.Result, error)
	Delete(ctx context.Context, set, flag string, query url.Values) error
	GetFlag(ctx context.Context, set, flag string) (flagservice.FlagView, error)
	GetSet(ctx context.Context, set string) (flagservice.SetView, error)
}

// Writer applies a Proposal through the flag service (W3, epic #1407): it writes
// the experimental value for (experiment id, arm "experimental") with
// PUT /game/{flag}. The flag service renders it as a targeting rung that only a
// game with game_kind != "real", that experiment id and that arm resolves
// (FR-138), so a real game's resolution cannot change by construction. The Gate
// checks that again on the rendered flag (a second check).
//
// The Writer never validates the invariant itself — that is the Gate's job.
// Apply assumes the Gate accepted the proposal; the Gate is the only
// sanctioned entry point (Gate.Review).
type Writer struct {
	svc FlagService
	log *slog.Logger
}

// NewWriter builds a Writer over a flag-service client. log nil uses slog.Default().
func NewWriter(svc FlagService, log *slog.Logger) *Writer {
	if log == nil {
		log = slog.Default()
	}
	return &Writer{svc: svc, log: log}
}

// NewWriterFromEnv builds a Writer for the flag service at FLAG_SERVICE_URL.
func NewWriterFromEnv(log *slog.Logger) *Writer {
	return NewWriter(flagservice.NewFromEnv(), log)
}

// Service returns the flag-service client (the Gate reads through the same one).
func (w *Writer) Service() FlagService { return w.svc }

// Apply writes the proposal's experimental value for its experiment's
// experimental arm and returns the flag service's WriteResult.
func (w *Writer) Apply(ctx context.Context, p Proposal) (flagservice.Result, error) {
	if p.ExperimentID == "" {
		return flagservice.Result{}, ErrUnscoped
	}
	return w.svc.Put(ctx, GameFlagSet, p.FlagKey, flagservice.Write{
		Value:  p.ExperimentalValue,
		Scope:  flagservice.Scope{ExperimentID: p.ExperimentID, Arm: ArmExperimental},
		Writer: experimentWriter,
		Reason: "experiment",
	})
}

// DryRun asks the service to validate and render the proposal's write without
// applying it; the result carries the flag before and after.
func (w *Writer) DryRun(ctx context.Context, p Proposal) (flagservice.Result, error) {
	if p.ExperimentID == "" {
		return flagservice.Result{}, ErrUnscoped
	}
	return w.svc.Put(ctx, GameFlagSet, p.FlagKey, flagservice.Write{
		Value:  p.ExperimentalValue,
		Scope:  flagservice.Scope{ExperimentID: p.ExperimentID, Arm: ArmExperimental},
		Writer: experimentWriter,
		Reason: "experiment",
		DryRun: true,
	})
}

// Strip removes one experiment's arm value — the teardown inverse of Apply
// (design §10). A flag or value that is already gone is a no-op, so Strip is
// idempotent (the registry calls it on every terminal status, possibly more
// than once). Removing an experiment value can only ever restore the
// pre-experiment resolution, so teardown does not go through the Gate.
func (w *Writer) Strip(ctx context.Context, flagKey, experimentID string) error {
	err := w.svc.Delete(ctx, GameFlagSet, flagKey, url.Values{
		"experiment_id": {experimentID},
		"arm":           {ArmExperimental},
		"writer":        {experimentWriter},
	})
	if flagservice.IsReason(err, "unknown_flag") {
		return nil // the surface it scoped is gone: nothing to strip
	}
	if err != nil {
		return fmt.Errorf("strip experiment %s from %s: %w", experimentID, flagKey, err)
	}
	return nil
}
