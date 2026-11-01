package decision

import (
	"context"
	"slices"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	otelmetric "go.opentelemetry.io/otel/metric"

	"github.com/joustmania/agent/decide"
	"github.com/joustmania/agent/flags"
	"github.com/joustmania/agent/gamecontext"
)

// The agent half of the group shield (#2467 PR 4; UC-040 A47, A48, BR-033;
// decide-contract §2, §4.3 to §4.5, §7.1). grant_group_shield shields 2 to
// policy.group_grant.max_targets alive players with one decision, all or nothing.
//
// The lever is a capability, not a rule: no rule proposes it. A remote backend may
// choose it when it is offered; nothing in the agent decides when a group shield is
// right. It is offered (and permitted) only while the group gate is open:
//
//	policy.group_grant.enabled on, AND
//	the game is a shadow game OR policy.group_grant.real_games is on (any other
//	kind, an unknown or empty one included, counts as real), AND
//	policy.group_grant.max_targets readable (2..6) and max_per_game at least 1, AND
//	2 <= alive players <= max_targets, AND
//	the game's group shields (written plus in flight) < max_per_game.
//
// The coordinator re-checks the same gate at the event (A48 step 1), so it stays
// authoritative; the agent's group count is in memory and lost on a restart, the
// coordinator's per-game backstop is not.

// Group block reasons of the permission chain (decision.block_reason). A per-target
// refusal of the shield ledger keeps the single grant's reason (already_charged,
// shield_cap_reached, shield_cooldown, shield_pool_full, shield_budget_exhausted)
// and names the refused serial (decision.group.refused_serial).
const (
	// ReasonGroupDisabled: the group gate is closed for this game (enabled off,
	// max_targets unreadable or max_per_game 0).
	ReasonGroupDisabled BlockReason = "group_disabled"
	// ReasonGroupRealGame: the game is not a shadow game and real_games is off.
	ReasonGroupRealGame BlockReason = "group_real_game"
	// ReasonGroupBadSet: the decision's set is not 2..max_targets distinct alive
	// players of the game.
	ReasonGroupBadSet BlockReason = "group_bad_set"
	// ReasonGroupCapReached: the game already has max_per_game group shields,
	// written or in flight.
	ReasonGroupCapReached BlockReason = "group_cap_reached"
)

// groupGate is the game-level half of the gate: "" when open, else the reason.
func groupGate(p flags.Policy, gameKind string) BlockReason {
	if !p.GroupGrantEnabled || p.GroupGrantMaxTargets < flags.GroupGrantMinTargets || p.GroupGrantMaxPerGame < 1 {
		return ReasonGroupDisabled
	}
	if gameKind != gamecontext.GameKindShadow && !p.GroupGrantRealGames {
		return ReasonGroupRealGame
	}
	return ""
}

// aliveSerials are the players c shows alive (Active true), as the decide state
// counts them (decide.BuildStateWith): the players a group option names.
func aliveSerials(c gamecontext.GameContext) []string {
	var out []string
	for key, p := range c.Players {
		if p == nil || p.Active == nil || !*p.Active {
			continue
		}
		serial := p.Serial
		if serial == "" {
			serial = key
		}
		out = append(out, serial)
	}
	slices.Sort(out)
	return out
}

// groupOffered reports whether the group lever may be offered in c's game now.
func (l *Loop) groupOffered(p flags.Policy, c gamecontext.GameContext) bool {
	if groupGate(p, c.GameKind) != "" {
		return false
	}
	alive := len(aliveSerials(c))
	if alive < flags.GroupGrantMinTargets || alive > p.GroupGrantMaxTargets {
		return false
	}
	return l.charges.groupCount() < p.GroupGrantMaxPerGame
}

// withoutClosedGroupLever returns allowed without the group levers when the group
// gate is closed for c (groupOffered), allowed unchanged otherwise or when it names
// none. Both paths build their questions from the result, so a closed gate offers
// no group lever and no group target option, and every existing request stays
// byte-identical (the rules golden's zero Policy keeps the gate closed).
func (l *Loop) withoutClosedGroupLever(p flags.Policy, c gamecontext.GameContext, allowed []string) []string {
	if !slices.ContainsFunc(allowed, decide.GroupTargeted) || l.groupOffered(p, c) {
		return allowed
	}
	out := make([]string, 0, len(allowed))
	for _, a := range allowed {
		if !decide.GroupTargeted(a) {
			out = append(out, a)
		}
	}
	return out
}

// groupSetRefusal checks a group decision's set against the gate and c: ""
// when the gate is open and the set is 2..max_targets distinct players c shows
// alive. The group count is checked under the ledger lock (reserveGroup).
func groupSetRefusal(p flags.Policy, c gamecontext.GameContext, d Decision) BlockReason {
	if r := groupGate(p, c.GameKind); r != "" {
		return r
	}
	n := len(d.TargetSerials)
	if n < flags.GroupGrantMinTargets || n > p.GroupGrantMaxTargets {
		return ReasonGroupBadSet
	}
	alive := aliveSerials(c)
	seen := make(map[string]bool, n)
	for _, s := range d.TargetSerials {
		if s == "" || seen[s] || !slices.Contains(alive, s) {
			return ReasonGroupBadSet
		}
		seen[s] = true
	}
	return ""
}

// groupPresent reports whether every target of a group decision is still
// in c (targetPresent, the landing check of a single target).
func groupPresent(c gamecontext.GameContext, serials []string) bool {
	for _, s := range serials {
		if !targetPresent(c, s) {
			return false
		}
	}
	return true
}

// shieldHold is a shield-ledger reservation the permission chain hands back:
// finish commits it when the write was applied and releases it otherwise. A nil
// *chargeReservation or *groupReservation is a valid, inert hold.
type shieldHold interface {
	finish(applied *bool)
}

// groupReservation reserves every target of one group shield and one slot of the
// game's group count, under one ledger generation.
type groupReservation struct {
	ledger  *chargeLedger
	serials []string
	gen     uint64
	reqs    []shieldRequest
	now     func() time.Time
}

// finish commits every target and the group slot when *applied, else releases
// them all. A nil reservation does nothing.
func (r *groupReservation) finish(applied *bool) {
	if r == nil {
		return
	}
	ok := applied != nil && *applied
	for i, s := range r.serials {
		if ok {
			r.ledger.commit(s, r.gen, r.now(), r.reqs[i])
		} else {
			r.ledger.release(s, r.gen)
		}
	}
	r.ledger.finishGroup(r.gen, ok)
}

// groupCount is the game's group shields, written plus in flight.
func (cl *chargeLedger) groupCount() int {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	return cl.groupCommitted + len(cl.groupPending)
}

// finishGroup commits or releases the group slot of generation gen.
func (cl *chargeLedger) finishGroup(gen uint64, applied bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	if !cl.groupPending[gen] {
		return
	}
	delete(cl.groupPending, gen)
	if applied {
		cl.groupCommitted++
	}
}

// reserveGroup atomically checks the game's group count and every target's entry
// (the same checks as a single grant: the hold pool, the trigger-mode charge, the
// cap, the cooldown) and reserves all of them, or none. On a refusal it returns the
// reason, the first refused serial ("" for the group count) and whether that
// refusal is the first of its kind against the entry.
func (cl *chargeLedger) reserveGroup(serials []string, now time.Time, reqs []shieldRequest, maxPerGame int) (res *groupReservation, reason BlockReason, refused string, firstRefusal bool) {
	cl.mu.Lock()
	defer cl.mu.Unlock()
	if cl.entries == nil {
		cl.entries = make(map[string]chargeEntry)
	}
	if len(reqs) > 0 {
		cl.limits = reqs[0].limits
	}
	if cl.groupCommitted+len(cl.groupPending) >= maxPerGame {
		first := !cl.groupCapReported
		cl.groupCapReported = true
		return nil, ReasonGroupCapReached, "", first
	}
	for i, s := range serials {
		e := cl.entries[s]
		r := reqs[i].hold.refusal(e.inFlight())
		if r == "" {
			r = e.refusal(now, reqs[i].limits, reqs[i].chargeWindow > 0)
		}
		if r != "" {
			first := e.reported != r
			e.reported = r
			cl.entries[s] = e
			return nil, r, s, first
		}
	}
	cl.gen++
	for i, s := range serials {
		e := cl.entries[s]
		if e.pending == nil {
			e.pending = make(map[uint64]time.Duration)
		}
		e.pending[cl.gen] = reqs[i].seconds
		e.reported = ""
		if reqs[i].chargeWindow > 0 {
			e.chargeGen, e.chargeCommitted, e.chargeUntil = cl.gen, false, now.Add(reqs[i].chargeWindow)
		}
		cl.entries[s] = e
	}
	if cl.groupPending == nil {
		cl.groupPending = make(map[uint64]bool)
	}
	cl.groupPending[cl.gen] = true
	cl.groupCapReported = false
	return &groupReservation{ledger: cl, serials: append([]string(nil), serials...), gen: cl.gen, reqs: reqs}, "", "", false
}

// reserveGroupShield is the shield-ledger step for a group decision: one request
// per target (its length, its hold pool, the trigger-mode charge window when the
// game is a real FFA game in trigger mode), then reserveGroup.
func (l *Loop) reserveGroupShield(ctx context.Context, snapshot flags.Snapshot, c gamecontext.GameContext, d Decision, now time.Time) (res *groupReservation, reason BlockReason, refused string, firstRefusal bool) {
	window, charge := l.chargeWindow(ctx, c)
	reqs := make([]shieldRequest, len(d.TargetSerials))
	for i, s := range d.TargetSerials {
		reqs[i] = shieldRequest{limits: limitsOf(snapshot.Policy), seconds: shieldSeconds(d), hold: holdPoolOf(c, s)}
		if charge {
			reqs[i].chargeWindow = window
		}
	}
	res, reason, refused, first := l.charges.reserveGroup(d.TargetSerials, now, reqs, snapshot.Policy.GroupGrantMaxPerGame)
	if res == nil {
		return nil, reason, refused, first
	}
	res.now = l.nowOrDefault()
	return res, "", "", false
}

// groupAttributes are the agent.decision attributes of a group decision.
func groupAttributes(d Decision, refused string, alive int) []attribute.KeyValue {
	attrs := []attribute.KeyValue{
		attribute.Int(AttrDecisionGroupSize, len(d.TargetSerials)),
		attribute.Int(AttrDecisionGroupAlive, alive),
		attribute.String(AttrDecisionGroupTargets, strings.Join(d.TargetSerials, ",")),
	}
	if refused != "" {
		attrs = append(attrs, attribute.String(AttrDecisionGroupRefusedSerial, refused))
	}
	return attrs
}

// Outcomes of agent_group_interventions_total (#2467 PR 6).
const (
	GroupOutcomeApplied  = "applied"
	GroupOutcomeBlocked  = "blocked"
	GroupOutcomeFailed   = "failed"
	GroupOutcomeGatedOff = "gated_off"
)

// recordGroupIntervention bumps agent_group_interventions_total for a group-shield
// decision; any other decision adds nothing. shape is "all" when the set is every
// alive player in c, "all_except" when it leaves at least one out. The backend is the
// one that answered ("rules" when none did). The counter is never nil.
func (l *Loop) recordGroupIntervention(ctx context.Context, c gamecontext.GameContext, state *LayerState, d Decision, outcome, reason string) {
	if d.Intervention != InterventionGrantGroupShield {
		return
	}
	backend := "rules"
	if state != nil && state.DecideBackend != "" {
		backend = state.DecideBackend
	}
	shape := "all_except"
	if len(d.TargetSerials) >= len(aliveSerials(c)) {
		shape = "all"
	}
	l.groupInterventions.Add(ctx, 1, otelmetric.WithAttributes(
		attribute.String("outcome", outcome),
		attribute.String("reason", reason),
		attribute.String("backend", backend),
		attribute.String("game_kind", gamecontext.TelemetryGameKind(c.GameKind)),
		attribute.String("shape", shape),
	))
}
