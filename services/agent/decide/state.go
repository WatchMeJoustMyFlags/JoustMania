package decide

import (
	"crypto/sha256"
	"encoding/hex"
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/joustmania/agent/gamecontext"
)

// BuildState turns a GameContext snapshot into the backend-facing State. It is
// pure and deterministic: the same snapshot always yields the same Narrative
// and Digest. Players are sorted by serial, the timeline renders through the
// shared gamecontext.RenderTimeline (the exact lines the llm prompt and retro
// use) with ages relative to c.UpdatedAt rather than the wall clock, and the
// Digest hashes a fixed-order canonical encoding that never ranges over a map.
func BuildState(c gamecontext.GameContext) State {
	return BuildStateWith(c, Annotations{})
}

// Annotations is what the decision loop knows about a game beyond its
// GameContext snapshot. The zero value adds nothing, so BuildStateWith(c,
// Annotations{}) equals BuildState(c).
type Annotations struct {
	// Charged holds the serials the agent's shield-charge ledger holds a live
	// entry for at the moment the state is built (UC-040 A31; #1870).
	Charged map[string]bool
	// ShieldInFlight holds, per serial, the seconds of the agent's grant_shield
	// writes in flight (reserved, not finished) at the moment the state is built. In
	// a hold game they are added to the player's energy (#2096; UC-040 A37).
	ShieldInFlight map[string]float64
	// NoShield holds the serials the agent would refuse a grant_shield to at the
	// moment the state is built, for any reason (Loop.ShieldSerials' no-shield set:
	// charged, capped, cooling down, and in a hold game a full pool or a used-up
	// budget). An alive player in it but not in Charged is marked ShieldLimited
	// (#2066; UC-040 A33, A37).
	NoShield map[string]bool
	// Levels holds the values the agent last wrote to this game, keyed
	// LevelKey(intervention, serial) (the applied-value ledger, Loop.AppliedValues;
	// UC-040 A35). The per-player sensitivity and handicap factors are read from it
	// (#2066).
	Levels map[string]string
}

// LevelKey is the Annotations.Levels key of an intervention and its target serial
// ("" for a game-wide intervention): the applied-value ledger's key.
func LevelKey(intervention, serial string) string { return intervention + "|" + serial }

// levelFactor is the per-player factor of intervention in levels for serial: the
// value as a finite number, or for an empty value (the writer then writes its
// default) the Default of the intervention's magnitude bounds; nil when absent,
// unparsable or the neutral value.
func levelFactor(levels map[string]string, intervention, serial string) *float64 {
	v, ok := levels[LevelKey(intervention, serial)]
	if !ok {
		return nil
	}
	b, hasBounds := MagnitudeBounds(intervention)
	var f float64
	if t := strings.TrimSpace(v); t == "" {
		if !hasBounds {
			return nil
		}
		f = b.Default
	} else {
		parsed, err := strconv.ParseFloat(t, 64)
		if err != nil || !finite(parsed) {
			return nil
		}
		f = parsed
	}
	if hasBounds && b.HasNeutral && f == b.Neutral {
		return nil
	}
	return &f
}

// BuildStateWith is BuildState plus the decision loop's annotations: an alive
// player in ann.Charged is marked ShieldCharged, and a grant_shield to them no
// longer counts as Shielded; an alive player in ann.NoShield but not in ann.Charged
// is marked ShieldLimited, and the non-neutral per-player factors in ann.Levels are
// set (#2066).
func BuildStateWith(c gamecontext.GameContext, ann Annotations) State {
	st := State{
		GameID:         c.SessionID,
		GameKind:       c.GameKind,
		Players:        buildPlayers(c, ann),
		ElapsedSeconds: elapsedSeconds(c),
	}
	applyHoldState(&st, c, ann)
	st.MostHeadroom, st.Struggling = applyDangerSignals(st.Players, c.Session.DeathThreshold)
	st.Narrative = renderNarrative(c, st)
	st.Digest = digest(st)
	return st
}

// AliveSerials returns the serials of the alive players, sorted.
func (s State) AliveSerials() []string {
	var out []string
	for _, p := range s.Players {
		if p.Alive {
			out = append(out, p.Serial)
		}
	}
	return out
}

func buildPlayers(c gamecontext.GameContext, ann Annotations) []PlayerState {
	elimOrder := make(map[string]int, len(c.Session.EliminationSequence))
	for i, serial := range c.Session.EliminationSequence {
		if _, seen := elimOrder[serial]; !seen {
			elimOrder[serial] = i + 1
		}
	}
	shielded := recentlyShielded(c, ann.Charged, c.Session.IsShieldHold())
	out := make([]PlayerState, 0, len(c.Players))
	for key, p := range c.Players {
		if p == nil {
			continue
		}
		serial := p.Serial
		if serial == "" {
			serial = key
		}
		alive := p.Active != nil && *p.Active
		ps := PlayerState{
			Serial:           serial,
			Alive:            alive,
			Intensity:        p.MovementIntensity,
			Variance:         p.MovementVariance,
			Skill:            p.SkillLevel,
			BatteryPct:       p.BatteryPct,
			EliminationOrder: elimOrder[serial],
			Shielded:         shielded[serial],
			ShieldCharged:    alive && ann.Charged[serial],
			ShieldLimited:    alive && ann.NoShield[serial] && !ann.Charged[serial],
		}
		if alive {
			ps.SensitivityFactor = levelFactor(ann.Levels, "adjust_player_sensitivity", serial)
			ps.HandicapFactor = levelFactor(ann.Levels, "set_player_handicap", serial)
		}
		out = append(out, ps)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Serial < out[j].Serial })
	return out
}

func elapsedSeconds(c gamecontext.GameContext) float64 {
	d := c.Session.DurationSeconds
	if d == nil || !finite(*d) || *d <= 0 {
		return 0
	}
	return *d
}

// renderNarrative is the line-oriented text a remote model reads. Numbers use
// fixed precision so the rendering is byte-stable for a given snapshot.
func renderNarrative(c gamecontext.GameContext, st State) string {
	var b strings.Builder
	b.WriteString("game: id=" + orUnknown(st.GameID) + " kind=" + orUnknown(st.GameKind) +
		" mode=" + orUnknown(deref(c.Session.GameMode)) +
		" elapsed=" + strconv.FormatFloat(st.ElapsedSeconds, 'f', 1, 64) + "s\n")

	b.WriteString("session: alive=" + strconv.Itoa(len(st.AliveSerials())) +
		" eliminated=" + strconv.Itoa(len(c.Session.EliminationSequence)))
	writeOpt(&b, "tempo", c.Session.MusicTempo)
	writeOpt(&b, "death_threshold", c.Session.DeathThreshold)
	if st.MostHeadroom != "" {
		b.WriteString(" most_headroom=" + st.MostHeadroom)
	}
	if st.Struggling != "" {
		b.WriteString(" struggling=" + st.Struggling)
	}
	b.WriteString(holdSessionNarrative(st))
	b.WriteString("\n")

	b.WriteString("players:\n")
	ranked := st.RankedCount()
	for _, p := range st.Players {
		status := "alive"
		if !p.Alive {
			status = "out"
			if p.EliminationOrder > 0 {
				status = "eliminated#" + strconv.Itoa(p.EliminationOrder)
			}
		}
		b.WriteString("  " + p.Serial + " " + status)
		writeOpt(&b, "intensity", p.Intensity)
		writeOpt(&b, "variance", p.Variance)
		writeOpt(&b, "skill", p.Skill)
		writeOpt(&b, "battery", p.BatteryPct)
		b.WriteString(holdNarrative(p, st))
		if src := c.Players[p.Serial]; src != nil && len(src.History) > 0 {
			b.WriteString(" movement=" + gamecontext.RenderSparkline(src.History, c.Session.DeathThreshold))
		}
		b.WriteString(dangerNarrative(p, ranked))
		b.WriteString(limitNarrative(p, st))
		b.WriteString("\n")
	}

	if lines := gamecontext.RenderTimeline(c.Timeline, c.UpdatedAt); len(lines) > 0 {
		b.WriteString("timeline:\n")
		for _, l := range lines {
			b.WriteString("  " + l + "\n")
		}
	}
	return b.String()
}

// digest hashes the canonical encoding of every State field except Digest
// itself. The encoding is a fixed field order with exact ('g', -1) float
// formatting and "-" for an unobserved signal, so it is stable across runs and
// distinguishes a missing reading from a zero. The narrative is included, which
// pulls in the session-level signals and the timeline it renders.
func digest(st State) string {
	var b strings.Builder
	b.WriteString("game_id=" + strconv.Quote(st.GameID) + "\n")
	b.WriteString("game_kind=" + strconv.Quote(st.GameKind) + "\n")
	b.WriteString("elapsed_s=" + strconv.FormatFloat(st.ElapsedSeconds, 'g', -1, 64) + "\n")
	for _, p := range st.Players {
		b.WriteString("player=" + strconv.Quote(p.Serial) +
			" alive=" + strconv.FormatBool(p.Alive) +
			" intensity=" + exact(p.Intensity) +
			" variance=" + exact(p.Variance) +
			" skill=" + exact(p.Skill) +
			" battery_pct=" + exact(p.BatteryPct) +
			" elimination_order=" + strconv.Itoa(p.EliminationOrder) +
			" intensity_rank=" + strconv.Itoa(p.IntensityRank) +
			" threshold_margin=" + exact(p.ThresholdMargin) +
			" danger=" + strconv.Quote(p.Danger) +
			" shielded=" + strconv.FormatBool(p.Shielded))
		// Written only when set, so a state without a charge keeps its digest.
		if p.ShieldCharged {
			b.WriteString(" shield_charged=true")
		}
		// #2066: likewise written only when set, so existing digests stay the same.
		if p.ShieldLimited {
			b.WriteString(" shield_limited=true")
		}
		if p.SensitivityFactor != nil {
			b.WriteString(" sensitivity_factor=" + exact(p.SensitivityFactor))
		}
		if p.HandicapFactor != nil {
			b.WriteString(" handicap_factor=" + exact(p.HandicapFactor))
		}
		// Hold mode only (#2096), so instant and trigger digests stay the same.
		if st.ShieldActivation != "" {
			b.WriteString(" shield_energy_s=" + exact(p.ShieldEnergyS) +
				" shield_budget_remaining_s=" + exact(p.ShieldBudgetRemainingS) +
				" shield_active=" + strconv.FormatBool(p.ShieldActive))
		}
		b.WriteString("\n")
	}
	if st.ShieldActivation != "" {
		b.WriteString("shield_activation=" + strconv.Quote(st.ShieldActivation) +
			" shield_energy_max_s=" + exact(st.ShieldEnergyMaxS) +
			" shield_budget_s=" + exact(st.ShieldBudgetS) +
			" shield_recharge_per_s=" + exact(st.ShieldRechargePerS) +
			" shield_min_burst_s=" + exact(st.ShieldMinBurstS) + "\n")
	}
	b.WriteString("most_headroom=" + strconv.Quote(st.MostHeadroom) + " struggling=" + strconv.Quote(st.Struggling) + "\n")
	b.WriteString("narrative=" + strconv.Quote(st.Narrative) + "\n")
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// limitNarrative is a player's shield-limit and applied-level suffix (#2066), e.g.
// " shield_limited sensitivity_factor=0.80", or " shield_limited=shield_pool_full"
// when the hold state itself gives the refusal. Empty when none is set.
func limitNarrative(p PlayerState, st State) string {
	var s string
	if p.ShieldLimited {
		s += " shield_limited"
		if r := st.HoldRefusal(p); r != "" {
			s += "=" + r
		}
	}
	if p.SensitivityFactor != nil {
		s += " sensitivity_factor=" + strconv.FormatFloat(*p.SensitivityFactor, 'f', 2, 64)
	}
	if p.HandicapFactor != nil {
		s += " handicap_factor=" + strconv.FormatFloat(*p.HandicapFactor, 'f', 2, 64)
	}
	return s
}

func exact(v *float64) string {
	if v == nil {
		return "-"
	}
	return strconv.FormatFloat(*v, 'g', -1, 64)
}

func writeOpt(b *strings.Builder, key string, v *float64) {
	if v == nil || math.IsNaN(*v) {
		return
	}
	b.WriteString(" " + key + "=" + strconv.FormatFloat(*v, 'f', 2, 64))
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func orUnknown(s string) string {
	if s == "" {
		return "unknown"
	}
	return s
}
