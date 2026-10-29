package actions

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/joustmania/agent/decision"
	"github.com/joustmania/agent/flagservice"
)

// The grant_shield length (UC-040 BR-029, #2395): the value of the variant named
// "default" of interventions/shield_seconds, read through the flag service at use
// time. The flag's defaultVariant stays "none" (0): it is what a player the agent
// never granted resolves, so the agent reads the named variant, not the default.
const (
	shieldSecondsVariant = "default"
	// shieldSecondsMin / shieldSecondsMax are the grant_shield magnitude bounds
	// (decide/ffa.go magnitudeBounds: 3 to 10 s), so the rules record's ffa.magnitude
	// answer is the written length (BR-029). A variant value outside them, or one that
	// is not a number, is not used. The registry accepts 1 to 10 (flags.md §13.1).
	shieldSecondsMin = 3.0
	shieldSecondsMax = 10.0
	// shieldSecondsReadTimeout bounds the read: the decision loop waits for it.
	shieldSecondsReadTimeout = 200 * time.Millisecond
	// shieldSecondsTTL is how long one read (or one failed read) serves: at most one
	// read per second for the whole agent, and a flag-service outage costs the loops
	// at most one timeout per second.
	shieldSecondsTTL = time.Second
	// shieldSecondsWarnEvery throttles the agent.shield_seconds_unreadable warning.
	shieldSecondsWarnEvery = 30 * time.Second
)

// FlagViewer is the part of the flag-service client the ShieldSecondsReader uses.
type FlagViewer interface {
	GetFlag(ctx context.Context, set, flag string) (flagservice.FlagView, error)
}

// ShieldSecondsReader resolves the grant_shield length (decision.ShieldSecondsSource).
// It fails closed to decision.DefaultShieldSeconds (5 s, the length before #2395) when
// the flag service cannot be read or the variant is missing or out of bounds. It is
// safe for concurrent use and shared by every loop.
type ShieldSecondsReader struct {
	svc FlagViewer
	log *slog.Logger
	now func() time.Time

	mu       sync.Mutex
	readAt   time.Time
	value    float64
	warnedAt time.Time
}

// NewShieldSecondsReader returns a reader over svc. A nil logger discards.
func NewShieldSecondsReader(svc FlagViewer, log *slog.Logger) *ShieldSecondsReader {
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	return &ShieldSecondsReader{svc: svc, log: log, now: time.Now}
}

// Seconds returns the grant length in seconds: the shield_seconds "default" variant,
// or decision.DefaultShieldSeconds when it cannot be used.
func (r *ShieldSecondsReader) Seconds(ctx context.Context) float64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	if !r.readAt.IsZero() && now.Sub(r.readAt) < shieldSecondsTTL {
		return r.value
	}
	v, err := r.read(ctx)
	if err != "" {
		v = decision.DefaultShieldSeconds
		if r.warnedAt.IsZero() || now.Sub(r.warnedAt) >= shieldSecondsWarnEvery {
			r.warnedAt = now
			r.log.Warn("agent.shield_seconds_unreadable", "reason", err, "fallback_seconds", v)
		}
	}
	r.readAt, r.value = now, v
	return v
}

// read returns the variant's value, or a reason it cannot be used.
func (r *ShieldSecondsReader) read(ctx context.Context) (float64, string) {
	if r.svc == nil {
		return 0, "no flag service"
	}
	rctx, cancel := context.WithTimeout(ctx, shieldSecondsReadTimeout)
	defer cancel()
	view, err := r.svc.GetFlag(rctx, FlagSetInterventions, flagShieldSeconds)
	if err != nil {
		return 0, "read failed"
	}
	raw, ok := view.VariantValues[shieldSecondsVariant]
	if !ok {
		return 0, "variant missing"
	}
	var v float64
	if json.Unmarshal(raw, &v) != nil {
		return 0, "not a number"
	}
	if v < shieldSecondsMin || v > shieldSecondsMax {
		return 0, fmt.Sprintf("out of bounds (value %v)", v)
	}
	return v, ""
}
