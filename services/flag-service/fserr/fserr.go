// Package fserr holds the flag service's rejection type: an HTTP status plus a
// reason code from services.md §7.2 ("Errors"). The reason is also the
// flagservice.reason span attribute and the reason label of
// flagservice_writes_total (telemetry.md §10).
package fserr

import (
	"errors"
	"fmt"
	"net/http"
)

// Reason codes (services.md §7.2).
const (
	Malformed           = "malformed"
	UnknownFlagSet      = "unknown_flag_set"
	UnknownFlag         = "unknown_flag"
	GameNotLive         = "game_not_live"
	SetNotServed        = "set_not_served"
	TooManyGames        = "too_many_games"
	GameIDTaken         = "game_id_taken"
	RealGameLive        = "real_game_live"
	WriterMismatch      = "writer_mismatch"
	FlagReadOnly        = "flag_read_only"
	OutOfBounds         = "out_of_bounds"
	SerialNotInGame     = "serial_not_in_game"
	NoSyncStream        = "no_sync_stream"
	Internal            = "internal"
	ReasonPersistFailed = "persist_failed"
	ReasonOK            = "ok"
	ReasonNotFound      = "not_found"
	ReasonRegistered    = "already_registered"
)

// Error is a rejected request.
type Error struct {
	Status int
	Reason string
	Detail string
}

func (e *Error) Error() string { return fmt.Sprintf("%d %s: %s", e.Status, e.Reason, e.Detail) }

func newf(status int, reason, format string, args ...any) *Error {
	return &Error{Status: status, Reason: reason, Detail: fmt.Sprintf(format, args...)}
}

// Constructors, one per status class.
func BadRequest(format string, args ...any) *Error {
	return newf(http.StatusBadRequest, Malformed, format, args...)
}
func NotFound(reason, format string, args ...any) *Error {
	return newf(http.StatusNotFound, reason, format, args...)
}
func Conflict(reason, format string, args ...any) *Error {
	return newf(http.StatusConflict, reason, format, args...)
}

// PreconditionFailed is a 412: a compare-and-delete precondition (DELETE
// ?if_writer=, #1877) did not hold, so nothing changed.
func PreconditionFailed(reason, format string, args ...any) *Error {
	return newf(http.StatusPreconditionFailed, reason, format, args...)
}
func Unprocessable(reason, format string, args ...any) *Error {
	return newf(http.StatusUnprocessableEntity, reason, format, args...)
}
func Unavailable(reason, format string, args ...any) *Error {
	return newf(http.StatusServiceUnavailable, reason, format, args...)
}

// PersistFailed is a 500: the state file could not be written, so the admin
// write was rolled back and nothing changed (#2368).
func PersistFailed(format string, args ...any) *Error {
	return newf(http.StatusInternalServerError, ReasonPersistFailed, format, args...)
}

// As extracts an *Error from err, wrapping any other error as 500 internal.
func As(err error) *Error {
	if err == nil {
		return nil
	}
	var e *Error
	if errors.As(err, &e) {
		return e
	}
	return &Error{Status: http.StatusInternalServerError, Reason: Internal, Detail: err.Error()}
}
