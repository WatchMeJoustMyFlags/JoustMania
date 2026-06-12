// Package gamesummary is the M7-1 game narrative builder (#928). After each game
// ends — real OR synthetic (shadow) — it AGGREGATES the partition's accumulated
// GameContext snapshot (the live signals plus the #916 rolling timeline) into one
// compact, self-describing Summary so the agent can later reason about WHAT
// HAPPENED (a narrative) instead of re-reading low-level spans/metrics.
//
// Boundaries (deliberately narrow, per #928):
//   - BuildSummary is a PURE function over a GameContext snapshot. It never reads
//     a clock, the network, or the filesystem — every time it needs is injected
//     (BuildOptions.Now) so the same snapshot always yields a byte-identical
//     Summary. The writer (writer.go) and the once-per-game span emission
//     (coordinator.go) are the only impure parts, and they are thin.
//   - It DERIVES rather than re-collects: engagement trend, elimination spread,
//     and the per-player elimination order all come straight off the #916
//     Timeline carried on the snapshot; player arcs and dominance fold the
//     live PlayerSignals together with the timeline. No new signal source is
//     introduced.
//
// Out of scope (later M7 issues, noted so a reader does not expect them here):
// the rolling WINDOW across multiple games (#929 M7-2 — this is ONE summary per
// game), prompt injection (#930), and the dashboard pane (#933). Deep causal
// before/after attribution beyond the best-effort timeline-window delta below is
// also deferred — see InterventionOutcome.Effect.
package gamesummary

import (
	"sort"
	"time"

	"github.com/joustmania/agent/gamecontext"
)

// SchemaVersion is bumped when the on-disk Summary JSON shape changes in a way a
// reader must notice. Carried on every file so #929's window builder and any
// offline tooling can branch on it rather than guess.
const SchemaVersion = 1

// effectWindow is the half-width of the timeline window the best-effort
// intervention before/after effect (effectDelta) compares: state-deltas within
// effectWindow BEFORE an intervention's timestamp form the "before" baseline,
// those within effectWindow AFTER form the "after". 30s comfortably spans a few
// state-delta entries on either side at the agent's dedupe cadence while staying
// local to the intervention, so the delta reflects THAT intervention rather than
// the whole game's drift. Pure constant — no clock involved.
const effectWindow = 30 * time.Second

// Summary is the compact, JSON-serialized game narrative (#928). One per game,
// keyed on GameID. Field names are stable wire contract: the dashboard (#933) and
// the #929 window builder read them, so renames are breaking.
type Summary struct {
	// SchemaVersion is SchemaVersion at write time (see the const).
	SchemaVersion int `json:"schema_version"`

	// GameID is the finished game's id (GameContext.SessionID, which since #845 IS
	// the real game_id once adopted). The on-disk filename is keyed off it.
	GameID string `json:"game_id"`
	// GameKind is "real" or "shadow" (or "" if never labeled). The builder runs for
	// BOTH so a reader can tell a live game from a synthetic evaluation one (#928).
	GameKind string `json:"game_kind"`
	// GameMode is the mode that was played (e.g. "joust"), or "" if unobserved.
	GameMode string `json:"game_mode,omitempty"`
	// GeneratedAt is the injected build time (BuildOptions.Now), RFC3339. The ONLY
	// place a wall-clock leaks into the Summary, and it is injected for determinism.
	GeneratedAt time.Time `json:"generated_at"`
	// DurationSeconds is the final observed game_duration_seconds, when known.
	DurationSeconds *float64 `json:"duration_seconds,omitempty"`

	// PlayerCount is the number of players the game saw (len of arcs).
	PlayerCount int `json:"player_count"`
	// EliminationCount is the number of eliminations recorded.
	EliminationCount int `json:"elimination_count"`
	// InterventionCount is the number of agent interventions the timeline captured
	// (dispatched + blocked).
	InterventionCount int `json:"intervention_count"`

	// PlayerArcs is the per-player trajectory list, sorted by serial for a stable
	// file. Each carries elimination/survival/final-state (#928 "player arcs").
	PlayerArcs []PlayerArc `json:"player_arcs"`
	// EliminationSpread is the ordered (time, serial) elimination distribution,
	// first-eliminated first (#928 "elimination spread").
	EliminationSpread []EliminationEvent `json:"elimination_spread"`
	// EngagementTrend is the down-sampled active-count / mean-intensity series the
	// game moved through, oldest-first (#928 "engagement trend"). Derived entirely
	// from the #916 timeline's state-delta entries.
	EngagementTrend []EngagementPoint `json:"engagement_trend"`
	// Dominance is the dominant-player detection result (#928 "dominant player").
	Dominance Dominance `json:"dominance"`
	// Interventions is the list of agent interventions that fired and their outcome
	// + best-effort before/after effect (#928 "intervention outcomes").
	Interventions []InterventionOutcome `json:"interventions"`
}

// PlayerArc is one player's trajectory through the game (#928 player arcs). All
// times are relative offsets from the game's first observed timeline event (that
// anchor is +0s) so the arc reads as "eliminated at +47s" independent of
// wall-clock — and stays deterministic.
//
// There is intentionally no join offset: per-player join time is not derivable
// from the data available here. State-delta timeline events are session-scoped
// (no serial), so a surviving player who is never the subject of a serial-scoped
// event (elimination / targeted intervention) has no join anchor at all. Adding a
// truthful join offset needs per-player join timestamps recorded at the Store
// level — a separate change, not faked from session-level deltas.
type PlayerArc struct {
	Serial string `json:"serial"`
	// Eliminated is true when the player appears in the elimination timeline.
	Eliminated bool `json:"eliminated"`
	// EliminationOrder is the 1-based order the player was eliminated, or 0 if the
	// player survived (never eliminated) — the survivor(s).
	EliminationOrder int `json:"elimination_order"`
	// EliminationOffsetSeconds is when the player was eliminated relative to game
	// start; nil for a survivor.
	EliminationOffsetSeconds *float64 `json:"elimination_offset_seconds,omitempty"`
	// SurvivalSeconds is how long the player lasted: elimination offset for an
	// eliminated player, or the full game span for a survivor. nil when neither can
	// be derived (no timeline anchor).
	SurvivalSeconds *float64 `json:"survival_seconds,omitempty"`
	// FinalSkill / FinalIntensity are the last observed per-player aggregates, when
	// known — the "how was this player doing" snapshot the arc closes on.
	FinalSkill     *float64 `json:"final_skill,omitempty"`
	FinalIntensity *float64 `json:"final_intensity,omitempty"`
}

// EliminationEvent is one (offset, serial, order) point in the elimination
// spread, derived from the #916 timeline's elimination entries.
type EliminationEvent struct {
	OffsetSeconds float64 `json:"offset_seconds"`
	Serial        string  `json:"serial"`
	Order         int     `json:"order"`
}

// EngagementPoint is one down-sampled state-delta from the #916 timeline: the
// active player count and mean movement intensity at OffsetSeconds into the game.
// Pointers distinguish "this delta did not carry that aggregate" from a real 0.
type EngagementPoint struct {
	OffsetSeconds float64  `json:"offset_seconds"`
	ActivePlayers *int     `json:"active_players,omitempty"`
	MeanIntensity *float64 `json:"mean_intensity,omitempty"`
}

// Dominance is the dominant-player detection (#928). A game is "dominated" when
// the survivor outlasted the field by a wide margin: the heuristic compares the
// survivor's survival time to the mean survival of the eliminated players. It is
// a coarse, explainable first cut (the Heuristic string names it) — not a model.
type Dominance struct {
	// Dominated is true when one player clearly ran away with the game.
	Dominated bool `json:"dominated"`
	// Serial is the dominant player's serial when Dominated; "" otherwise.
	Serial string `json:"serial,omitempty"`
	// Heuristic names the rule that decided, so a reader knows WHY (e.g.
	// "survivor_outlasted_mean_2x"). Always set, even when not dominated.
	Heuristic string `json:"heuristic"`
	// Margin is the survival-time ratio (survivor / mean-eliminated) that fed the
	// decision, for transparency; 0 when not computable.
	Margin float64 `json:"margin,omitempty"`
}

// InterventionOutcome records one agent intervention the timeline captured and a
// best-effort before/after effect (#928 intervention outcomes). The intervention
// stream is the #916 timeline's EventIntervention entries (Store.
// AppendInterventionEvent): the decision Loop's LayerState.Outcomes is the live
// source, but those entries are not on the game-end snapshot, whereas the timeline
// IS — so the summary reads them from the narrative the snapshot carries. Today
// that seam is unwired (see Store.AppendInterventionEvent's #916 deferral), so in
// production this list is usually empty until the seam is threaded; the aggregator
// handles them correctly the moment they appear (and the tests drive them).
type InterventionOutcome struct {
	Intervention string `json:"intervention"`
	// TargetSerial is the player the intervention targeted, or "" for a session-
	// scoped one.
	TargetSerial string `json:"target_serial,omitempty"`
	// Dispatched is true when the intervention reached the action sink; false when a
	// permission/policy gate blocked it.
	Dispatched bool `json:"dispatched"`
	// OffsetSeconds is when the intervention fired, relative to game start.
	OffsetSeconds float64 `json:"offset_seconds"`
	// Effect is the BEST-EFFORT before/after engagement delta around the
	// intervention (see effectDelta). nil when the timeline lacks state-deltas on
	// both sides of the intervention to compare — full causal attribution is
	// DEFERRED (#928 out-of-scope); this is a coarse correlation, not causation.
	Effect *InterventionEffect `json:"effect,omitempty"`
}

// InterventionEffect is the best-effort timeline-window delta around an
// intervention: the change in mean intensity / active count between the
// effectWindow before and after the intervention's timestamp. It is a CORRELATION
// (the engagement moved by this much around the intervention), explicitly NOT a
// causal claim — deeper attribution is deferred per #928.
type InterventionEffect struct {
	// MeanIntensityDelta is after-mean minus before-mean of the state-delta
	// intensities in the windows; nil when either side had no intensity sample.
	MeanIntensityDelta *float64 `json:"mean_intensity_delta,omitempty"`
	// ActivePlayersDelta is after minus before active-count (last sample each side);
	// nil when either side had no active-count sample.
	ActivePlayersDelta *int `json:"active_players_delta,omitempty"`
}

// BuildOptions injects everything impure into the otherwise-pure builder.
type BuildOptions struct {
	// Now is the build timestamp stamped on the Summary (GeneratedAt). Injected so
	// the build is deterministic; the aggregation logic itself never reads it.
	Now time.Time
}

// BuildSummary aggregates a game-end GameContext snapshot into a Summary (#928).
// It is PURE: deterministic over (c, opts), no clock/network/disk. The snapshot is
// expected to be the pre-reset one the Store hands OnGameEnd (SessionID +
// EliminationSequence + Timeline intact).
func BuildSummary(c gamecontext.GameContext, opts BuildOptions) Summary {
	// anchor is the game's first timeline event time — the +0s origin every offset
	// is measured from. Falls back to UpdatedAt when the timeline is empty so a
	// game with no narrative still produces well-formed (all-zero-offset) output.
	anchor, lastAt := timelineBounds(c.Timeline)

	elims := buildEliminationSpread(c.Timeline, anchor)
	trend := buildEngagementTrend(c.Timeline, anchor)
	interventions := buildInterventions(c.Timeline, anchor)
	arcs := buildPlayerArcs(c, elims, anchor, lastAt)
	dom := detectDominance(arcs)

	return Summary{
		SchemaVersion:     SchemaVersion,
		GameID:            c.SessionID,
		GameKind:          c.GameKind,
		GameMode:          deref(c.Session.GameMode),
		GeneratedAt:       opts.Now,
		DurationSeconds:   c.Session.DurationSeconds,
		PlayerCount:       len(arcs),
		EliminationCount:  len(elims),
		InterventionCount: len(interventions),
		PlayerArcs:        arcs,
		EliminationSpread: elims,
		EngagementTrend:   trend,
		Dominance:         dom,
		Interventions:     interventions,
	}
}

// timelineBounds returns the first and last event timestamps in the timeline, or
// two zero times when it is empty. The first is the +0s anchor for every offset.
func timelineBounds(events []gamecontext.TimelineEvent) (first, last time.Time) {
	if len(events) == 0 {
		return time.Time{}, time.Time{}
	}
	// events() hands them oldest-first, so the bounds are the ends of the slice.
	return events[0].At, events[len(events)-1].At
}

// offset returns the whole-fractional seconds from anchor to t. A zero anchor
// (empty timeline) yields 0 so offsets stay well-defined.
func offset(t, anchor time.Time) float64 {
	if anchor.IsZero() || t.IsZero() {
		return 0
	}
	return t.Sub(anchor).Seconds()
}

// buildEliminationSpread folds the timeline's elimination entries into the ordered
// (offset, serial, order) spread, first-eliminated first (#928). Order comes from
// the entry's 1-based Order; the slice is sorted by it so the spread is stable
// regardless of timeline append order.
func buildEliminationSpread(events []gamecontext.TimelineEvent, anchor time.Time) []EliminationEvent {
	out := make([]EliminationEvent, 0)
	for _, e := range events {
		if e.Kind != gamecontext.EventElimination {
			continue
		}
		out = append(out, EliminationEvent{
			OffsetSeconds: offset(e.At, anchor),
			Serial:        e.Serial,
			Order:         e.Order,
		})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Order < out[j].Order })
	return out
}

// buildEngagementTrend folds the timeline's state-delta entries into the
// engagement series (#928). Each delta is already change-deduped by the Store
// (recordStateDelta only appends on a tracked-aggregate change), so the trend is
// a compact TREND log, not a per-signal dump — no further down-sampling needed.
func buildEngagementTrend(events []gamecontext.TimelineEvent, anchor time.Time) []EngagementPoint {
	out := make([]EngagementPoint, 0)
	for _, e := range events {
		if e.Kind != gamecontext.EventStateDelta {
			continue
		}
		out = append(out, EngagementPoint{
			OffsetSeconds: offset(e.At, anchor),
			ActivePlayers: copyIntPtr(e.ActivePlayers),
			MeanIntensity: copyFloatPtr(e.MeanIntensity),
		})
	}
	return out
}

// buildInterventions folds the timeline's intervention entries into the outcome
// list with a best-effort before/after effect (#928). The effect compares the
// engagement-trend windows on either side of each intervention's timestamp.
func buildInterventions(events []gamecontext.TimelineEvent, anchor time.Time) []InterventionOutcome {
	out := make([]InterventionOutcome, 0)
	for _, e := range events {
		if e.Kind != gamecontext.EventIntervention {
			continue
		}
		out = append(out, InterventionOutcome{
			Intervention:  e.Detail,
			TargetSerial:  e.Serial,
			Dispatched:    !e.Blocked,
			OffsetSeconds: offset(e.At, anchor),
			Effect:        effectDelta(events, e.At),
		})
	}
	return out
}

// effectDelta computes the best-effort before/after engagement effect of an
// intervention at time at: it averages the state-delta intensities in the
// effectWindow BEFORE at vs AFTER at and reports the difference, and likewise the
// last active-count on each side. It returns nil when neither aggregate has a
// sample on BOTH sides — there is then nothing honest to compare, and full causal
// attribution is deferred (#928). This is a CORRELATION around the intervention,
// not a causal claim.
func effectDelta(events []gamecontext.TimelineEvent, at time.Time) *InterventionEffect {
	var beforeSum, afterSum float64
	var beforeN, afterN int
	var beforeActive, afterActive *int

	for _, e := range events {
		if e.Kind != gamecontext.EventStateDelta {
			continue
		}
		d := e.At.Sub(at)
		switch {
		case d < 0 && -d <= effectWindow:
			if e.MeanIntensity != nil {
				beforeSum += *e.MeanIntensity
				beforeN++
			}
			if e.ActivePlayers != nil {
				beforeActive = e.ActivePlayers // last (closest) before wins via iteration order
			}
		case d >= 0 && d <= effectWindow:
			if e.MeanIntensity != nil {
				afterSum += *e.MeanIntensity
				afterN++
			}
			if e.ActivePlayers != nil && afterActive == nil {
				afterActive = e.ActivePlayers // first (closest) after wins
			}
		}
	}

	eff := &InterventionEffect{}
	any := false
	if beforeN > 0 && afterN > 0 {
		delta := afterSum/float64(afterN) - beforeSum/float64(beforeN)
		eff.MeanIntensityDelta = &delta
		any = true
	}
	if beforeActive != nil && afterActive != nil {
		delta := *afterActive - *beforeActive
		eff.ActivePlayersDelta = &delta
		any = true
	}
	if !any {
		return nil
	}
	return eff
}

// buildPlayerArcs folds the live PlayerSignals together with the elimination
// spread and the timeline into one arc per player (#928). Survival is the
// elimination offset for an eliminated player, or the full game span
// (anchor..lastAt) for a survivor. (No join offset — see PlayerArc: per-player
// join time is not derivable from session-scoped state deltas.)
func buildPlayerArcs(c gamecontext.GameContext, elims []EliminationEvent, anchor, lastAt time.Time) []PlayerArc {
	// Index the elimination spread by serial for O(1) arc lookup.
	elimBySerial := make(map[string]EliminationEvent, len(elims))
	for _, e := range elims {
		elimBySerial[e.Serial] = e
	}

	// The set of serials the game saw is the union of live players and any serial
	// that only ever appears as an elimination target (a player evicted before the
	// snapshot but still narrated). This keeps a late-evicted player in the arc.
	serials := make(map[string]struct{})
	for serial := range c.Players {
		serials[serial] = struct{}{}
	}
	for _, e := range elims {
		serials[e.Serial] = struct{}{}
	}

	gameSpan := offset(lastAt, anchor) // full game length in seconds (0 if no timeline)

	arcs := make([]PlayerArc, 0, len(serials))
	for serial := range serials {
		arc := PlayerArc{Serial: serial}
		if p := c.Players[serial]; p != nil {
			arc.FinalSkill = copyFloatPtr(p.SkillLevel)
			arc.FinalIntensity = copyFloatPtr(p.MovementIntensity)
		}
		if elim, ok := elimBySerial[serial]; ok {
			arc.Eliminated = true
			arc.EliminationOrder = elim.Order
			off := elim.OffsetSeconds
			arc.EliminationOffsetSeconds = &off
			surv := off // survival == time from start to elimination
			arc.SurvivalSeconds = &surv
		} else if !lastAt.IsZero() {
			// Survivor: lasted the whole observed game.
			surv := gameSpan
			arc.SurvivalSeconds = &surv
		}
		arcs = append(arcs, arc)
	}
	// Stable, serial-sorted output so the file is byte-identical for a given input.
	sort.Slice(arcs, func(i, j int) bool { return arcs[i].Serial < arcs[j].Serial })
	return arcs
}

// detectDominance applies the dominant-player heuristic (#928): a survivor who
// outlasted the eliminated field by >= dominanceRatio times its mean survival
// "ran away with the game". Coarse and explainable by design; the Heuristic field
// names the rule so a reader knows exactly what fired.
func detectDominance(arcs []PlayerArc) Dominance {
	const dominanceRatio = 2.0
	const heuristic = "survivor_outlasted_mean_2x"

	var survivor *PlayerArc
	var elimSurvivalSum float64
	var elimCount int
	survivors := 0
	for i := range arcs {
		a := &arcs[i]
		if a.Eliminated {
			if a.SurvivalSeconds != nil {
				elimSurvivalSum += *a.SurvivalSeconds
				elimCount++
			}
			continue
		}
		survivors++
		survivor = a
	}

	// Dominance requires exactly one survivor measured against an eliminated field.
	if survivors != 1 || survivor == nil || survivor.SurvivalSeconds == nil || elimCount == 0 {
		return Dominance{Heuristic: heuristic}
	}
	meanElim := elimSurvivalSum / float64(elimCount)
	if meanElim <= 0 {
		return Dominance{Heuristic: heuristic}
	}
	margin := *survivor.SurvivalSeconds / meanElim
	return Dominance{
		Dominated: margin >= dominanceRatio,
		Serial:    dominantSerial(survivor, margin >= dominanceRatio),
		Heuristic: heuristic,
		Margin:    margin,
	}
}

// dominantSerial returns the survivor's serial only when the game was dominated,
// so the field is "" on a non-dominated game (the JSON omits it).
func dominantSerial(survivor *PlayerArc, dominated bool) string {
	if !dominated {
		return ""
	}
	return survivor.Serial
}

// deref returns the pointed-to string, or "" for a nil pointer.
func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// copyIntPtr / copyFloatPtr return a fresh pointer to the same value, so the
// Summary never aliases the snapshot's pointers (the snapshot shares pointers with
// nothing live, but copying keeps the Summary self-contained and JSON-stable).
func copyIntPtr(p *int) *int {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}

func copyFloatPtr(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}
