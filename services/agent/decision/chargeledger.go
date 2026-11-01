package decision

import (
	"context"
	"math"
	"strconv"
	"sync"
	"time"

	"github.com/joustmania/agent/decide"
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
// In a hold game (#2096; UC-040 A37 step 1) a grant_shield adds energy to the
// player's pool. The charge check does not apply; instead, before the cap and the
// cooldown, the grant is refused shield_budget_exhausted or shield_pool_full from the
// coordinator's game_player_shield_* gauges (decide.HoldShieldRefusal), counting the
// seconds of this player's grants in flight. The mode comes from the
// game_shield_activation_mode gauge, never from flagd.
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

// DefaultShieldSeconds is the fail-closed grant_shield length (UC-040 BR-029, #2395):
// the loop stamps a grant with no value with the shield_seconds "default" variant
// (SetShieldSecondsSource) and falls back to this when that cannot be read; the writer
// (actions/writer.go) and the ledger use it for a decision that still carries no
// value, or one that does not parse (UC-040 BR-022). It equals the variant's baseline.
const DefaultShieldSeconds = 5

// ShieldSecondsSource returns the grant_shield length in seconds for a decision that
// carries no value (UC-040 BR-029, #2395). main.go wires actions.ShieldSecondsReader,
// which reads the interventions/shield_seconds "default" variant through the flag
// service at use time and fails closed to DefaultShieldSeconds, so it always returns a
// usable length.
type ShieldSecondsSource func(ctx context.Context) float64

// SetShieldSecondsSource injects the grant length source. nil (the construction
// default) keeps a grant without a value empty. Set during construction.
func (l *Loop) SetShieldSecondsSource(src ShieldSecondsSource) { l.shieldSeconds = src }

// withShieldSeconds stamps a targeted grant_shield that carries no value (every rules
// decision, a remote answer without a magnitude) with the configured length, so the
// ledger's cooldown end and in-flight seconds and the writer use one value (UC-040
// BR-029). The source is read only for a grant the allow-list permits; a value the
// decision already carries (a remote magnitude) is kept.
func (l *Loop) withShieldSeconds(ctx context.Context, snapshot flags.Snapshot, d Decision) Decision {
	if l.shieldSeconds == nil || d.Value != "" || !grantsShieldLength(d) {
		return d
	}
	if !snapshot.Permits(d.Intervention) {
		return d
	}
	v := l.shieldSeconds(ctx)
	if math.IsNaN(v) || math.IsInf(v, 0) || v <= 0 {
		v = DefaultShieldSeconds
	}
	d.Value = strconv.FormatFloat(v, 'g', -1, 64)
	return d
}

// grantsShieldLength reports whether d grants shields whose length the loop stamps:
// a targeted grant_shield, or a grant_group_shield with its set (#2467: the group's
// targets share one length, which the ledger and the writer's seconds both use).
func grantsShieldLength(d Decision) bool {
	switch d.Intervention {
	case InterventionGrantShield:
		return d.TargetSerial != ""
	case InterventionGrantGroupShield:
		return len(d.TargetSerials) >= 2
	}
	return false
}

// stampShieldSeconds stamps the grant_shield length on the rules decisions of one
// cycle before they run (withShieldSeconds), and makes the rules record's
// ffa.magnitude answer report the stamped length of the decision its answers
// describe (the first one), so the decide span and record say what is written, not
// the 5 s bounds default (UC-040 BR-029, decide contract §4; #2395). runDecision
// keeps a value already stamped, so the source is read once per grant.
func (l *Loop) stampShieldSeconds(ctx context.Context, snapshot flags.Snapshot, ds []Decision, rec *decideRecord) []Decision {
	if l.shieldSeconds == nil || len(ds) == 0 {
		return ds
	}
	out := make([]Decision, len(ds))
	for i, d := range ds {
		out[i] = l.withShieldSeconds(ctx, snapshot, d)
	}
	if rec != nil && rec.hasResp {
		rec.resp = magnitudeFromDecision(rec.req, rec.resp, out[0])
	}
	return out
}

// magnitudeFromDecision returns resp with its ffa.magnitude answer set to d's value,
// normalised over d's bounds, when resp chose d's intervention and d carries a
// numeric value. Otherwise resp is returned unchanged.
func magnitudeFromDecision(req decide.Request, resp decide.Response, d Decision) decide.Response {
	iv, ok := resp.Find(decide.QuestionFFAIntervention)
	if !ok || iv.Choice != d.Intervention {
		return resp
	}
	v, err := strconv.ParseFloat(d.Value, 64)
	if err != nil {
		return resp
	}
	norm, ok := decide.NormalizeMagnitude(d.Intervention, v)
	if !ok {
		return resp
	}
	var q *decide.Question
	for i := range req.Questions {
		if req.Questions[i].ID == decide.QuestionFFAMagnitude && req.Questions[i].Kind == decide.KindScore {
			q = &req.Questions[i]
		}
	}
	if q == nil {
		return resp
	}
	answers := make([]decide.Answer, len(resp.Answers))
	copy(answers, resp.Answers)
	for i := range answers {
		if answers[i].QuestionID == decide.QuestionFFAMagnitude {
			answers[i].Score = q.Range[0] + norm*(q.Range[1]-q.Range[0])
		}
	}
	resp.Answers = answers
	return resp
}

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
	pending   map[uint64]time.Duration
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
	// The game's group shields (#2467, UC-040 A47, BR-033): written, in flight
	// (by reservation generation), and whether the last group_cap_reached refusal
	// was already reported (a repeat stays quiet, as A21).
	groupCommitted   int
	groupPending     map[uint64]bool
	groupCapReported bool
}

// shieldRequest is what one targeted grant_shield asks the ledger for: a charge
// window (trigger mode only, 0 = no charge check), the hold-mode pool of the target
// (hold games only, nil = no hold check), the live limits and the granted length.
type shieldRequest struct {
	chargeWindow time.Duration
	hold         *holdPool
	limits       shieldLimits
	seconds      time.Duration
}

// holdPool is a hold-mode player's pool as the game context shows it (#2096).
type holdPool struct {
	energy, budgetLeft, cap, minBurst *float64
	active                            bool
}

// holdPoolOf returns serial's pool in c, nil outside a hold game or for a player the
// context does not show alive.
func holdPoolOf(c gamecontext.GameContext, serial string) *holdPool {
	if !c.Session.IsShieldHold() {
		return nil
	}
	p := c.Players[serial]
	if p == nil || p.Active == nil || !*p.Active {
		return nil
	}
	return &holdPool{
		energy:     p.ShieldEnergy,
		budgetLeft: p.ShieldBudgetRemaining,
		cap:        c.Session.ShieldSetting(gamecontext.ShieldSettingEnergyMax),
		minBurst:   c.Session.ShieldSetting(gamecontext.ShieldSettingMinBurst),
		active:     p.ShieldActive != nil && *p.ShieldActive,
	}
}

// refusal is the hold check of the pool with inFlight more seconds on the way.
func (h *holdPool) refusal(inFlight time.Duration) BlockReason {
	if h == nil {
		return ""
	}
	return BlockReason(decide.HoldShieldRefusal(h.energy, h.budgetLeft, h.active, h.cap, h.minBurst, inFlight.Seconds()))
}

// inFlight is the seconds of the entry's grants reserved and not finished yet.
func (e chargeEntry) inFlight() time.Duration {
	var sum time.Duration
	for _, seconds := range e.pending {
		sum += seconds
	}
	return sum
}

// reserve atomically checks serial's entry and reserves it. It returns exactly one
// of: a refusal (reason set, res nil; firstRefusal reports whether it is the first
// with that reason since the last reservation or write) or a reservation.
func (cl *chargeLedger) reserve(serial string, now time.Time, req shieldRequest) (res *chargeReservation, reason BlockReason, firstRefusal bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	cl.limits = req.limits
	if cl.entries == nil {
		// A hold refusal can be the first record of a serial (#2096).
		cl.entries = make(map[string]chargeEntry)
	}
	e := cl.entries[serial]
	r := req.hold.refusal(e.inFlight())
	if r == "" {
		r = e.refusal(now, req.limits, req.chargeWindow > 0)
	}
	if r != "" {
		first := e.reported != r
		e.reported = r
		cl.entries[serial] = e
		return nil, r, first
	}
	cl.gen++
	if e.pending == nil {
		e.pending = make(map[uint64]time.Duration)
	}
	e.pending[cl.gen] = req.seconds
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

// ShieldSerials is the rules engine's view of the ledger for the game context c,
// taken once per ranking (UC-040 A31, A33, A37): charged feeds the decide state the
// ranking builds, noShield (charged plus the players at the shield cap or in the
// cooldown, and in a hold game the players whose pool is full or whose budget is used
// up, #2096) feeds the rules' no-shield filter, so R4 does not propose a shield the
// ledger would refuse.
func (l *Loop) ShieldSerials(c gamecontext.GameContext) (charged, noShield map[string]bool) {
	charged, noShield = l.charges.views(l.nowOrDefault()())
	if !c.Session.IsShieldHold() {
		return charged, noShield
	}
	inFlight := l.charges.inFlight()
	for serial := range c.Players {
		if l.holdRefusal(c, serial, inFlight[serial]) == "" {
			continue
		}
		if noShield == nil {
			noShield = make(map[string]bool, len(c.Players))
		}
		noShield[serial] = true
	}
	return charged, noShield
}

func (l *Loop) holdRefusal(c gamecontext.GameContext, serial string, inFlight time.Duration) BlockReason {
	return holdPoolOf(c, serial).refusal(inFlight)
}

// ShieldInFlight is the seconds of the agent's grant_shield writes in flight per
// serial (reserved, not finished): the decide state adds them to a hold-mode
// player's energy (#2096). nil when none.
func (l *Loop) ShieldInFlight() map[string]float64 {
	var out map[string]float64
	for serial, d := range l.charges.inFlight() {
		if out == nil {
			out = make(map[string]float64)
		}
		out[serial] = d.Seconds()
	}
	return out
}

// inFlight returns, from one lock, the seconds in flight per serial (only serials
// with some).
func (cl *chargeLedger) inFlight() map[string]time.Duration {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	var out map[string]time.Duration
	for serial, e := range cl.entries {
		if d := e.inFlight(); d > 0 {
			if out == nil {
				out = make(map[string]time.Duration)
			}
			out[serial] = d
		}
	}
	return out
}

// ShieldGrants is the rules engine's catch-up view of the ledger (UC-040 A41, BR-030;
// #2395): the agent's grant_shield writes per serial in this game, the ones in flight
// included (so two concurrent cycles do not both pick the same player with the fewest).
// The ledger is per Loop and a Loop is per game id, so the count starts at 0 in every
// game and a shadow game never sees a real game's grants. nil when there are none.
func (l *Loop) ShieldGrants() map[string]int {
	return l.charges.grants()
}

// grants returns, from one lock, committed plus in-flight grants per serial (only
// serials with some).
func (cl *chargeLedger) grants() map[string]int {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	var out map[string]int
	for serial, e := range cl.entries {
		if n := e.committed + len(e.pending); n > 0 {
			if out == nil {
				out = make(map[string]int, len(cl.entries))
			}
			out[serial] = n
		}
	}
	return out
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
// trigger. The cheap checks run first, so the flag is read only for such a grant. A
// game latched to hold (game_shield_activation_mode, #2096) has no charges, whatever
// the flag reads now.
func (l *Loop) chargeLedgerApplies(ctx context.Context, c gamecontext.GameContext, d Decision) (time.Duration, bool) {
	if d.Intervention != InterventionGrantShield || d.TargetSerial == "" {
		return 0, false
	}
	return l.chargeWindow(ctx, c)
}

// chargeWindow reports whether a shield granted in c's game is a trigger-mode charge
// and the window to use: a real Free-for-All game whose shield.activation resolves to
// trigger, not latched to hold. A group shield's targets use it too (#2467).
func (l *Loop) chargeWindow(ctx context.Context, c gamecontext.GameContext) (time.Duration, bool) {
	if l.shieldChargeMode == nil {
		return 0, false
	}
	if c.Session.IsShieldHold() {
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
// charge (already_charged, A31) or in a hold game the pool (shield_budget_exhausted,
// shield_pool_full, A37), the cap (shield_cap_reached) and the cooldown
// (shield_cooldown, A33), and reserves. It returns the reservation to finish (nil
// when the ledger does not apply or refused), the refusal reason ("" when not
// refused) and whether that refusal is the first of its kind against the entry.
func (l *Loop) reserveShield(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time) (res *chargeReservation, reason BlockReason, firstRefusal bool) {
	if d.Intervention != InterventionGrantShield || d.TargetSerial == "" {
		return nil, "", false
	}
	req := shieldRequest{limits: limitsOf(snapshot.Policy), seconds: shieldSeconds(d), hold: holdPoolOf(c, d.TargetSerial)}
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
