package decision

import (
	"context"
	"sync"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// The agent's shield-charge ledger (UC-040 A31 step 1, BR-020; #1837 PR-4a).
//
// In trigger mode a grant_shield gives the player a charge the coordinator holds until
// the player activates it, it expires or the player dies. The agent cannot see that
// state (it observes only OTLP and has no shield-state gauge), so it keeps its own
// record of the charges it WROTE: at most one agent charge per player per window. A
// second grant_shield to the same player inside the window is blocked already_charged
// before the rate limit, so it charges no budget and the coordinator never sees it.
//
// The ledger lives in the per-game Loop (one Loop per game id, so a new game starts with
// an empty ledger) under its own mutex, on the loop clock. It is in memory only: an
// agent restart loses it and the coordinator's own already_charged check (A31 step 2)
// is the backstop.

// shieldTriggerGameMode is the only game mode in which shield.activation=trigger
// applies (UC-040 BR-019; games/ffa.py SHIELD_TRIGGER_ALLOWED, get_game_name()).
// tests/contract/test_shield_trigger_modes.py fails when the coordinator's allow-list
// and this constant drift apart.
const shieldTriggerGameMode = "FFA"

// chargeLedgerCap caps the window for a long or never-expiring charge: past it the
// agent may grant again and the coordinator refuses the grant if the charge is still
// held (A31 step 2).
const chargeLedgerCap = 20 * time.Second

// chargeLedgerMargin covers the time from the agent's write to the coordinator's apply
// (NFR-050 propagation, CI p99 1.5 s, rounded up). The coordinator's expiry runs from
// its apply, the agent's window from its write. A constant, not a tunable (C-035).
const chargeLedgerMargin = 2 * time.Second

// ShieldChargeMode reports, for one game, whether shield.activation resolves to
// trigger and the charge expiry (0 = never). main.go wires flags.Flags.ShieldCharge,
// which fails open (false) when the game flag set cannot be read.
type ShieldChargeMode func(ctx context.Context, gameID, gameMode string) (trigger bool, expiry time.Duration)

// SetShieldChargeMode injects the shield-mode source of the charge ledger. nil (the
// construction default, the golden harness and every older test) keeps the ledger
// inert: no grant_shield is ever blocked already_charged. Set during construction.
func (l *Loop) SetShieldChargeMode(mode ShieldChargeMode) { l.shieldChargeMode = mode }

// chargeEntry is one player's ledger entry. Every entry, a reserved one too, carries
// until: a reservation whose finish never ran blocks for at most one window.
type chargeEntry struct {
	gen       uint64
	committed bool
	until     time.Time
	// refusalReported is set by the first refusal against this entry: later refusals
	// are repeats and stay quiet (A31 step 1, as A21).
	refusalReported bool
}

// chargeLedger is the per-Loop record of the charges the agent wrote, by serial.
type chargeLedger struct {
	mu      sync.Mutex
	entries map[string]chargeEntry
	gen     uint64
}

// reserve atomically checks and reserves serial's entry. A live entry (now before its
// until) refuses: res is nil and firstRefusal reports whether this is the first refusal
// against that entry. Otherwise a new entry with a fresh generation is reserved.
func (cl *chargeLedger) reserve(serial string, now time.Time, window time.Duration) (res *chargeReservation, firstRefusal bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	if e, ok := cl.entries[serial]; ok && now.Before(e.until) {
		first := !e.refusalReported
		e.refusalReported = true
		cl.entries[serial] = e
		return nil, first
	}
	if cl.entries == nil {
		cl.entries = make(map[string]chargeEntry)
	}
	cl.gen++
	cl.entries[serial] = chargeEntry{gen: cl.gen, until: now.Add(window)}
	return &chargeReservation{ledger: cl, serial: serial, gen: cl.gen, window: window}, false
}

// commit keeps the entry of generation gen and starts its window at now (the write).
// A stale generation (the entry was replaced) does nothing.
func (cl *chargeLedger) commit(serial string, gen uint64, now time.Time, window time.Duration) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	if e, ok := cl.entries[serial]; ok && e.gen == gen {
		e.committed = true
		e.until = now.Add(window)
		cl.entries[serial] = e
	}
}

// release drops the entry of generation gen; a stale generation does nothing.
func (cl *chargeLedger) release(serial string, gen uint64) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	if e, ok := cl.entries[serial]; ok && e.gen == gen {
		delete(cl.entries, serial)
	}
}

// chargeReservation is the handle of one reserved entry. runDecision defers finish
// right after the permission chain, so every path out of it commits or releases.
type chargeReservation struct {
	ledger *chargeLedger
	serial string
	gen    uint64
	window time.Duration
	now    func() time.Time
}

// finish commits the entry when *applied (the write went out), else releases it. A
// nil reservation (the ledger did not apply to this decision) does nothing.
func (r *chargeReservation) finish(applied *bool) {
	if r == nil {
		return
	}
	if applied != nil && *applied {
		r.ledger.commit(r.serial, r.gen, r.now(), r.window)
		return
	}
	r.ledger.release(r.serial, r.gen)
}

// chargeLedgerWindow is min(expiry, cap) (cap when the expiry is 0, i.e. never) plus
// the propagation margin.
func chargeLedgerWindow(expiry time.Duration) time.Duration {
	if expiry <= 0 || expiry > chargeLedgerCap {
		expiry = chargeLedgerCap
	}
	return expiry + chargeLedgerMargin
}

// chargeLedgerApplies reports whether the ledger governs d and the window to use: a
// targeted grant_shield in a real Free-for-All game whose shield.activation resolves to
// trigger. The cheap checks run first, so the flag is read only for such a grant.
func (l *Loop) chargeLedgerApplies(ctx context.Context, c gamecontext.GameContext, d Decision) (time.Duration, bool) {
	if l.shieldChargeMode == nil || d.Intervention != InterventionGrantShield || d.TargetSerial == "" {
		return 0, false
	}
	mode := derefStr(c.Session.GameMode)
	if c.GameKind != gamecontext.GameKindReal || mode != shieldTriggerGameMode {
		return 0, false
	}
	trigger, expiry := l.shieldChargeMode(ctx, c.SessionID, mode)
	if !trigger {
		return 0, false
	}
	return chargeLedgerWindow(expiry), true
}

// reserveCharge runs the ledger step of the permission chain (decide-contract §4.5
// step 3). It returns the reservation to finish (nil when the ledger does not apply or
// refused), whether d is refused already_charged, and whether that refusal is the first
// against the entry.
func (l *Loop) reserveCharge(ctx context.Context, c gamecontext.GameContext, d Decision, now time.Time) (res *chargeReservation, refused, firstRefusal bool) {
	window, ok := l.chargeLedgerApplies(ctx, c, d)
	if !ok {
		return nil, false, false
	}
	res, first := l.charges.reserve(d.TargetSerial, now, window)
	if res == nil {
		return nil, true, first
	}
	res.now = l.nowOrDefault()
	return res, false, false
}
