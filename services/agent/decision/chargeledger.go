package decision

import (
	"context"
	"math"
	"strconv"
	"sync"
	"time"

	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// The agent's shield ledger: the shield-charge ledger (UC-040 A31 step 1, BR-020;
// #1837 PR-4a) merged with the per-player shield cap and cooldown (UC-040 A33,
// BR-022; #2044). One entry per serial, one reserve and one finish, so a refusal of
// one check can never leave a reservation of another behind.
//
// Shield limits: the agent counts the grant_shield writes it made to each player in
// this game and remembers the end of the last one (write + granted seconds + the
// propagation margin, in trigger mode too: the cooldown runs from the grant). A grant
// is refused shield_cap_reached at policy.max_shields_per_player_per_game (written plus
// in flight) and shield_cooldown while now is before that end plus
// policy.shield_cooldown_seconds, or while another grant to the player is in flight.
// Both limits are read from the cycle's live snapshot; 0 = unlimited / off, and a
// hand-built Snapshot (the rules golden) leaves them 0, so nothing is refused. The
// count is an upper bound of the coordinator's (BR-022): it also counts writes the
// coordinator refused or kept, so the agent errs towards fewer shields.
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
type ShieldChargeMode func(ctx context.Context, gameID string) (trigger bool, expiry time.Duration)

// SetShieldChargeMode injects the shield-mode source of the charge ledger. nil (the
// construction default, the golden harness and every older test) keeps the ledger
// inert: no grant_shield is ever blocked already_charged. Set during construction.
func (l *Loop) SetShieldChargeMode(mode ShieldChargeMode) { l.shieldChargeMode = mode }

// DefaultShieldSeconds is the grant_shield length when the decision carries no
// value (or one that does not parse): the writer writes it (actions/writer.go), and
// the ledger takes the shield's end from it (UC-040 BR-022). It matches the
// interventions set's shield_seconds "default" variant.
const DefaultShieldSeconds = 5

// shieldLimits are the live shield limits of one check (UC-040 A33, BR-022): the
// policy.max_shields_per_player_per_game and policy.shield_cooldown_seconds values of
// the snapshot. 0 = unlimited / off; both 0 (the zero value) refuse nothing.
type shieldLimits struct {
	maxShields int
	cooldown   time.Duration
}

func limitsOf(p flags.Policy) shieldLimits {
	return shieldLimits{maxShields: p.MaxShieldsPerPlayerPerGame, cooldown: time.Duration(p.ShieldCooldownSeconds) * time.Second}
}

// chargeEntry is one player's shield-ledger entry for this game: the trigger-mode
// charge window (A31) and the shield count and last end (A33, BR-022). An entry is
// never deleted during a game, so the count holds; the Loop is per game, so a new
// game starts empty.
type chargeEntry struct {
	// chargeGen is the generation of the reservation that set chargeUntil (0: none).
	chargeGen       uint64
	chargeCommitted bool
	// chargeUntil ends the charge window. Every charge, a reserved one too, carries
	// it: a reservation whose finish never ran blocks for at most one window.
	chargeUntil time.Time
	// committed counts the agent's grant_shield writes to this player in this game
	// (applied=true); pending holds the generations of the reservations not finished
	// yet. The cap counts both, so two concurrent cycles cannot both take the last
	// slot.
	committed int
	pending   map[uint64]struct{}
	// lastEnd is the latest end of the written shields: write + granted seconds +
	// chargeLedgerMargin (in trigger mode too: the cooldown runs from the grant).
	lastEnd time.Time
	// reported is the reason of the last refusal against this entry since the last
	// reservation or write: a refusal with the same reason is a repeat and stays
	// quiet (A31 step 1, A33, as A21).
	reported BlockReason
}

// refusal returns the reason reserve refuses this entry at now under lim, or "".
// The order is already_charged (only when the charge check applies: trigger mode),
// shield_cap_reached, shield_cooldown; a pending reservation refuses shield_cooldown
// while a cooldown is set (a grant in flight has no end yet, and two back-to-back
// grants would extend the shield).
func (e chargeEntry) refusal(now time.Time, lim shieldLimits, chargeApplies bool) BlockReason {
	if chargeApplies && now.Before(e.chargeUntil) {
		return ReasonAlreadyCharged
	}
	if lim.maxShields > 0 && e.committed+len(e.pending) >= lim.maxShields {
		return ReasonShieldCapReached
	}
	if lim.cooldown > 0 && (len(e.pending) > 0 || (!e.lastEnd.IsZero() && now.Before(e.lastEnd.Add(lim.cooldown)))) {
		return ReasonShieldCooldown
	}
	return ""
}

// chargeLedger is the per-Loop shield ledger by serial: the charges the agent wrote
// (A31) and its shields per player in this game (A33). One mutex, so a check and the
// views (ChargedSerials, ShieldSerials) each see one consistent state.
type chargeLedger struct {
	mu      sync.Mutex
	entries map[string]chargeEntry
	gen     uint64
	// limits are the limits of the latest cycle or check: the views use them.
	limits shieldLimits
}

// shieldRequest is what one targeted grant_shield asks the ledger for: a charge
// window (trigger mode only, 0 = no charge check), the live limits and the granted
// length.
type shieldRequest struct {
	chargeWindow time.Duration
	limits       shieldLimits
	seconds      time.Duration
}

// reserve atomically checks serial's entry and reserves it. It returns exactly one
// of: a refusal (reason set, res nil; firstRefusal reports whether it is the first
// with that reason since the last reservation or write) or a reservation.
func (cl *chargeLedger) reserve(serial string, now time.Time, req shieldRequest) (res *chargeReservation, reason BlockReason, firstRefusal bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	cl.limits = req.limits
	e := cl.entries[serial]
	if r := e.refusal(now, req.limits, req.chargeWindow > 0); r != "" {
		first := e.reported != r
		e.reported = r
		cl.entries[serial] = e
		return nil, r, first
	}
	if cl.entries == nil {
		cl.entries = make(map[string]chargeEntry)
	}
	cl.gen++
	if e.pending == nil {
		e.pending = make(map[uint64]struct{})
	}
	e.pending[cl.gen] = struct{}{}
	e.reported = ""
	if req.chargeWindow > 0 {
		e.chargeGen, e.chargeCommitted, e.chargeUntil = cl.gen, false, now.Add(req.chargeWindow)
	}
	cl.entries[serial] = e
	return &chargeReservation{ledger: cl, serial: serial, gen: cl.gen, req: req}, "", false
}

// commit records the write of reservation gen at now: the charge window restarts at
// the write and the shield counts, its end being now + seconds + margin. A stale
// generation (already finished) does nothing.
func (cl *chargeLedger) commit(serial string, gen uint64, now time.Time, req shieldRequest) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	e, ok := cl.entries[serial]
	if !ok {
		return
	}
	if _, live := e.pending[gen]; !live {
		return
	}
	delete(e.pending, gen)
	if e.chargeGen == gen {
		e.chargeCommitted = true
		e.chargeUntil = now.Add(req.chargeWindow)
	}
	e.committed++
	// Keep the later end: with the cooldown off two grants can be in flight and commit
	// out of order, and a shorter shield must not move the end back.
	if end := now.Add(req.seconds + chargeLedgerMargin); end.After(e.lastEnd) {
		e.lastEnd = end
	}
	e.reported = ""
	cl.entries[serial] = e
}

// release drops reservation gen: its charge (if it still owns it) and its pending
// slot. The count and the last end stay. A stale generation does nothing.
func (cl *chargeLedger) release(serial string, gen uint64) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	e, ok := cl.entries[serial]
	if !ok {
		return
	}
	if _, live := e.pending[gen]; !live {
		return
	}
	delete(e.pending, gen)
	if e.chargeGen == gen {
		e.chargeGen, e.chargeCommitted, e.chargeUntil = 0, false, time.Time{}
	}
	cl.entries[serial] = e
}

// setLimits records the cycle's live limits for the views.
func (cl *chargeLedger) setLimits(lim shieldLimits) {
	cl.mu.Lock()
	cl.limits = lim
	cl.mu.Unlock()
}

// live returns the serials whose charge, reserved or committed, is live at now:
// exactly the serials reserve would refuse already_charged at now. nil when there are
// none. It takes only the ledger's own mutex and calls nothing else, so a caller may
// hold another lock.
func (cl *chargeLedger) live(now time.Time) map[string]bool {
	charged, _ := cl.views(now)
	return charged
}

// views returns, from one lock, the charged serials (live) and the no-shield set:
// every serial a targeted grant_shield would be refused for at now under the latest
// limits (already_charged, shield_cap_reached or shield_cooldown). Either is nil when
// empty.
func (cl *chargeLedger) views(now time.Time) (charged, noShield map[string]bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	for serial, e := range cl.entries {
		// A live charge counts whatever the mode reads now (A31: entries written
		// before a trigger -> instant flip stay live for their window).
		r := e.refusal(now, cl.limits, true)
		if r == "" {
			continue
		}
		if noShield == nil {
			noShield = make(map[string]bool, len(cl.entries))
		}
		noShield[serial] = true
		if r == ReasonAlreadyCharged {
			if charged == nil {
				charged = make(map[string]bool, len(cl.entries))
			}
			charged[serial] = true
		}
	}
	return charged, noShield
}

// ChargedSerials is the ledger view the decide state reads (#1870): the players the
// ledger would refuse a grant_shield to already_charged right now, on the loop clock
// (the clock of every entry's until). Empty outside trigger mode: the ledger then
// holds no charge, apart from charges written before a trigger -> instant flip, which
// end with their window (at most 22 s).
func (l *Loop) ChargedSerials() map[string]bool {
	return l.charges.live(l.nowOrDefault()())
}

// ShieldSerials is the rules engine's view of the ledger, taken once per ranking
// (UC-040 A31, A33): charged feeds the decide state the ranking builds, noShield
// (charged plus the players at the shield cap or in the cooldown) feeds the rules'
// no-shield filter, so R4 does not propose a shield the ledger would refuse.
func (l *Loop) ShieldSerials() (charged, noShield map[string]bool) {
	return l.charges.views(l.nowOrDefault()())
}

// chargeReservation is the handle of one reservation. runDecision defers finish
// right after the permission chain, so every path out of it commits or releases,
// a refusal after the ledger step included.
type chargeReservation struct {
	ledger *chargeLedger
	serial string
	gen    uint64
	req    shieldRequest
	now    func() time.Time
}

// finish commits the reservation when *applied (the write went out), else releases
// it. A nil reservation (the ledger did not apply to this decision) does nothing.
func (r *chargeReservation) finish(applied *bool) {
	if r == nil {
		return
	}
	if applied != nil && *applied {
		r.ledger.commit(r.serial, r.gen, r.now(), r.req)
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
	trigger, expiry := l.shieldChargeMode(ctx, c.SessionID)
	if !trigger {
		return 0, false
	}
	return chargeLedgerWindow(expiry), true
}

// shieldSeconds is the length of the shield d grants: its value, or
// DefaultShieldSeconds when empty or not a finite non-negative number (as the writer).
func shieldSeconds(d Decision) time.Duration {
	v, err := strconv.ParseFloat(d.Value, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) || v < 0 {
		v = DefaultShieldSeconds
	}
	return time.Duration(v * float64(time.Second))
}

// reserveShield runs the shield-ledger step of the permission chain (decide-contract
// §4.5 step 4): for a targeted grant_shield it checks, in order, the trigger-mode
// charge (already_charged, A31), the cap (shield_cap_reached) and the cooldown
// (shield_cooldown, A33), and reserves. It returns the reservation to finish (nil
// when the ledger does not apply or refused), the refusal reason ("" when not
// refused) and whether that refusal is the first of its kind against the entry.
func (l *Loop) reserveShield(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time) (res *chargeReservation, reason BlockReason, firstRefusal bool) {
	if d.Intervention != InterventionGrantShield || d.TargetSerial == "" {
		return nil, "", false
	}
	req := shieldRequest{limits: limitsOf(snapshot.Policy), seconds: shieldSeconds(d)}
	if window, ok := l.chargeLedgerApplies(ctx, c, d); ok {
		req.chargeWindow = window
	}
	res, reason, first := l.charges.reserve(d.TargetSerial, now, req)
	if res == nil {
		return nil, reason, first
	}
	res.now = l.nowOrDefault()
	return res, "", false
}
