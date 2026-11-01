// Package gamewindow is the cross-game context window (#929, reshaped by #1368).
// It keeps, in agent-process memory only, recent finished games and renders them
// into the PRIOR GAMES block the shadow-experiment proposer reads (UC-042 step 4).
// It is the agent's only memory that crosses a game boundary, so it is where C-009
// ("No player identity") is enforced:
//
//   - A controller is not a person, not even across games: the same controller in
//     the next game may be held by someone else. So a game enters the window as a
//     GameAggregate built from game-level numbers only (mode, player count,
//     duration, outcome type, death and near-death rates, interventions by type
//     with their measured effect). The per-controller fields of the game summary
//     (serials, player arcs, dominance serial, intervention target serials) are
//     dropped at Record time and never stored here (UC-042 BR-004).
//   - Real games and shadow games never mix (UC-042 BR-005). Real games feed one
//     real-game ring. A shadow game feeds only the linked set of its experiment
//     (keyed by experiment_id, UC-043), and is visible only to games of that same
//     experiment; the set is evicted when the experiment concludes (Forget). A
//     shadow game with no experiment has no linked set and is dropped.
//   - Pattern-driven synthetic controllers (UC-046) have no person behind them: the
//     "player" is the generating function. Inside a linked set their per-player
//     rows may cross games, keyed by archetype name + params digest + slot, never
//     by serial (serials are reassigned and are hardware identifiers). Rows come
//     only from the pattern game's replay record, which lists pattern controllers
//     only, so a non-pattern controller never contributes a row (UC-042 BR-006).
//
// The window is GLOBAL (one Store shared by every per-game loop), mutex-guarded,
// and lost on agent restart. Nothing is persisted to disk.
package gamewindow

import (
	"sort"
	"sync"

	"github.com/joustmania/agent/gamecontext"
	"github.com/joustmania/agent/gamesummary"
	"github.com/joustmania/agent/patterngame"
)

// RetentionCap bounds each ring: the real-game ring and each linked set's game and
// pattern-game lists hold at most this many entries; one more evicts the oldest
// (UC-042 BR-005).
const RetentionCap = 20

// MaxLinkedSets bounds how many experiments' linked sets are held at once, a
// safety net for an experiment whose conclusion was never signalled; past it the
// least recently created set is evicted. It exceeds the registry's live
// experiment cap.
const MaxLinkedSets = 64

// NearDeathRatio is the closest-call ratio (intensity / death threshold) at or
// above which a surviving player counts as a near-death for NearDeathRate.
const NearDeathRatio = 0.8

// Outcome types of a game. The agent sees no end reason, so the type is derived
// from the survivors: one survivor is a winner, none is a tie, and more than one is
// a force-end or a team win (the agent cannot tell those apart).
const (
	OutcomeWinner            = "winner"
	OutcomeTie               = "tie"
	OutcomeMultipleSurvivors = "multiple_survivors"
	OutcomeUnknown           = "unknown"
)

// GameAggregate is one game as the window keeps it: game-level numbers only.
// It deliberately has no field that can hold a serial, a name or any other
// per-controller value (UC-042 BR-004).
type GameAggregate struct {
	Mode            string
	PlayerCount     int
	DurationSeconds *float64
	Outcome         string
	// EliminationCount / PlayerCount; nil when the game saw no players.
	DeathRate *float64
	// Share of the players with an observed closest call who survived the game
	// with a closest call at or above NearDeathRatio; nil when no closest call was
	// observed.
	NearDeathRate *float64
	Interventions []InterventionAggregate
	// PredictionTally is the game's prediction tally per backend, source and
	// template (UC-042 A12 step 3). The scored predictions themselves are not kept:
	// a target-scope one names a serial (BR-004).
	PredictionTally []PredictionAggregate
}

// PredictionAggregate is one (backend, source, template, acted) group of a game's
// scored predictions: counts only. Backend, source and template are closed
// vocabularies (decide backends, prediction sources and templates), never a serial
// or a name.
type PredictionAggregate struct {
	Backend    string
	Source     string
	Template   string
	Acted      bool
	Hits       int
	Misses     int
	Unscorable int
	BrierSum   float64
}

// InterventionAggregate is one intervention type within a game: how often it was
// applied or blocked and its mean measured effect (no target).
type InterventionAggregate struct {
	Type string
	// Applied: written. Blocked: refused by the permission chain or turned into a
	// no-op by interventions_enabled (the GAME_SUMMARY split, #1771, #1778). A
	// dispatched decision that was a no-op or a failed write is in neither.
	Applied int
	Blocked int
	// MeanIntensityDelta / MeanActiveDelta average the best-effort before/after
	// effects of this type's interventions; nil when none had that effect measured.
	MeanIntensityDelta *float64
	MeanActiveDelta    *float64
}

// ShadowGame is one game of a linked set: its arm plus its game-level aggregate.
type ShadowGame struct {
	Arm  string
	Game GameAggregate
}

// PatternGame is one pattern game's per-player rows (UC-046), keyed by archetype.
type PatternGame struct {
	Arm          string
	PairIndex    int // -1 when the game is not part of a seed-matched pair
	Archetype    string
	ParamsDigest string
	Outcome      string
	Censored     bool
	Players      []PatternPlayer
}

// PatternPlayer is one pattern-driven controller, identified by its slot in the
// archetype (the generating function's index), never by serial.
type PatternPlayer struct {
	Slot int
	// EliminationPlace is the 1-based elimination position (1 = first out); 0 for
	// a controller that was not eliminated.
	EliminationPlace int
	Won              bool
	// SurvivalSeconds is frames delivered / stream rate; nil when the rate is unknown.
	SurvivalSeconds *float64
}

// LinkedSet is the shadow history of one experiment, oldest-first.
type LinkedSet struct {
	Games        []ShadowGame
	PatternGames []PatternGame
}

// Window is what one game may see: the real-game ring and, for a shadow game,
// its own experiment's linked set (nil otherwise). Oldest-first.
type Window struct {
	Games  []GameAggregate
	Linked *LinkedSet
}

// Store is the global window. Safe for concurrent use.
type Store struct {
	mu    sync.Mutex
	games []GameAggregate
	// linked holds one set per experiment_id; linkedOrder is creation order for
	// the MaxLinkedSets safety eviction.
	linked      map[string]*LinkedSet
	linkedOrder []string
	// concluded remembers recently forgotten experiments so a game that ends just
	// after its experiment concluded does not recreate the set (bounded).
	concluded      map[string]struct{}
	concludedOrder []string
}

// NewStore builds an empty window.
func NewStore() *Store {
	return &Store{
		games:     make([]GameAggregate, 0, RetentionCap),
		linked:    make(map[string]*LinkedSet),
		concluded: make(map[string]struct{}),
	}
}

// Record is the gamesummary.Observer seam. A real game is reduced to its
// GameAggregate and appended to the real-game ring; a shadow game goes to its
// experiment's linked set only; anything else is dropped (UC-042 BR-004, BR-005).
func (s *Store) Record(summary gamesummary.Summary) {
	agg := FromSummary(summary)
	s.mu.Lock()
	defer s.mu.Unlock()
	switch summary.GameKind {
	case gamecontext.GameKindReal:
		s.games = appendBounded(s.games, agg)
	case gamecontext.GameKindShadow:
		if set := s.setLocked(summary.ExperimentID); set != nil {
			set.Games = appendBounded(set.Games, ShadowGame{Arm: summary.Arm, Game: agg})
		}
	}
}

// RecordPattern adds a finished pattern game's per-slot rows to its experiment's
// linked set (UC-042 BR-006). A pattern game with no experiment is dropped.
func (s *Store) RecordPattern(rec patterngame.Record) {
	pg := FromPatternRecord(rec)
	s.mu.Lock()
	defer s.mu.Unlock()
	if set := s.setLocked(rec.ExperimentID); set != nil {
		set.PatternGames = appendBounded(set.PatternGames, pg)
	}
}

// Forget evicts an experiment's linked set when the experiment concludes (UC-042
// BR-005). Later records for that experiment are dropped.
func (s *Store) Forget(experimentID string) {
	if experimentID == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.dropSetLocked(experimentID)
	if _, ok := s.concluded[experimentID]; ok {
		return
	}
	s.concluded[experimentID] = struct{}{}
	s.concludedOrder = append(s.concludedOrder, experimentID)
	if len(s.concludedOrder) > MaxLinkedSets {
		delete(s.concluded, s.concludedOrder[0])
		s.concludedOrder = s.concludedOrder[1:]
	}
}

// setLocked returns (creating if needed) the linked set of experimentID, or nil
// when there is no experiment or it already concluded.
func (s *Store) setLocked(experimentID string) *LinkedSet {
	if experimentID == "" {
		return nil
	}
	if _, done := s.concluded[experimentID]; done {
		return nil
	}
	if set, ok := s.linked[experimentID]; ok {
		return set
	}
	if len(s.linkedOrder) >= MaxLinkedSets {
		s.dropSetLocked(s.linkedOrder[0])
	}
	set := &LinkedSet{}
	s.linked[experimentID] = set
	s.linkedOrder = append(s.linkedOrder, experimentID)
	return set
}

func (s *Store) dropSetLocked(experimentID string) {
	if _, ok := s.linked[experimentID]; !ok {
		return
	}
	delete(s.linked, experimentID)
	for i, id := range s.linkedOrder {
		if id == experimentID {
			s.linkedOrder = append(s.linkedOrder[:i:i], s.linkedOrder[i+1:]...)
			break
		}
	}
}

// SnapshotFor returns what a game of the given kind and experiment may see: the
// real-game ring always, plus the game's own linked set when it is a shadow game
// of an experiment (UC-042 BR-005). Copies, oldest-first.
func (s *Store) SnapshotFor(gameKind, experimentID string) Window {
	s.mu.Lock()
	defer s.mu.Unlock()
	w := Window{Games: append([]GameAggregate{}, s.games...)}
	if gameKind != gamecontext.GameKindShadow || experimentID == "" {
		return w
	}
	if set, ok := s.linked[experimentID]; ok {
		w.Linked = &LinkedSet{
			Games:        append([]ShadowGame{}, set.Games...),
			PatternGames: append([]PatternGame{}, set.PatternGames...),
		}
	}
	return w
}

// Len reports how many real games the window holds (0..RetentionCap).
func (s *Store) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.games)
}

// LinkedSets reports how many experiments currently have a linked set.
func (s *Store) LinkedSets() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.linked)
}

// appendBounded appends v and evicts the oldest past RetentionCap, copying into a
// fresh backing array so evicted entries are not pinned alive.
func appendBounded[T any](ring []T, v T) []T {
	ring = append(ring, v)
	if len(ring) <= RetentionCap {
		return ring
	}
	trimmed := make([]T, RetentionCap)
	copy(trimmed, ring[len(ring)-RetentionCap:])
	return trimmed
}

// FromSummary reduces a per-game summary to game-level aggregates (UC-042 BR-004).
// Every per-controller field of the summary is read only to count and is dropped.
func FromSummary(s gamesummary.Summary) GameAggregate {
	agg := GameAggregate{
		Mode:            s.GameMode,
		PlayerCount:     s.PlayerCount,
		DurationSeconds: copyFloat(s.DurationSeconds),
		Outcome:         outcomeOf(s.PlayerCount, s.SurvivorCount),
		Interventions:   aggregateInterventions(s.Interventions, s.InterventionsByType),
	}
	if s.PlayerCount > 0 {
		rate := float64(s.EliminationCount) / float64(s.PlayerCount)
		agg.DeathRate = &rate
	}
	agg.NearDeathRate = nearDeathRate(s.PlayerArcs)
	if len(s.PredictionTally) > 0 {
		for _, t := range s.PredictionTally {
			agg.PredictionTally = append(agg.PredictionTally, PredictionAggregate{
				Backend: t.Backend, Source: t.Source, Template: t.Template, Acted: t.Acted,
				Hits: t.Hits, Misses: t.Misses, Unscorable: t.Unscorable, BrierSum: t.BrierSum,
			})
		}
	}
	return agg
}

// outcomeOf derives the outcome type from the summary's survivor count, which
// comes from the session's unbounded elimination sequence (not the 64-entry
// timeline).
func outcomeOf(players, survivors int) string {
	if players == 0 {
		return OutcomeUnknown
	}
	switch survivors {
	case 0:
		return OutcomeTie
	case 1:
		return OutcomeWinner
	default:
		return OutcomeMultipleSurvivors
	}
}

func nearDeathRate(arcs []gamesummary.PlayerArc) *float64 {
	observed, near := 0, 0
	for _, a := range arcs {
		if a.NearDeathRatio == nil {
			continue
		}
		observed++
		if !a.Eliminated && *a.NearDeathRatio >= NearDeathRatio {
			near++
		}
	}
	if observed == 0 {
		return nil
	}
	rate := float64(near) / float64(observed)
	return &rate
}

// aggregateInterventions builds the per-type intervention aggregates. Counts come
// from the whole-game tally when present (the timeline is a 64-entry ring and can
// lose early interventions); the mean effects come from the interventions still on
// the timeline, the only ones with a measured before/after window.
func aggregateInterventions(ivs []gamesummary.InterventionOutcome, tally []gamesummary.InterventionTypeCount) []InterventionAggregate {
	type acc struct {
		agg                     InterventionAggregate
		intensitySum, activeSum float64
		intensityN, activeN     int
	}
	byType := map[string]*acc{}
	for _, iv := range ivs {
		a := byType[iv.Intervention]
		if a == nil {
			a = &acc{agg: InterventionAggregate{Type: iv.Intervention}}
			byType[iv.Intervention] = a
		}
		// The timeline cannot tell a written dispatch from a gated no-op, so only
		// its blocked entries count here; applied comes from the tally.
		if !iv.Dispatched {
			a.agg.Blocked++
		}
		if iv.Effect == nil {
			continue
		}
		if iv.Effect.MeanIntensityDelta != nil {
			a.intensitySum += *iv.Effect.MeanIntensityDelta
			a.intensityN++
		}
		if iv.Effect.ActivePlayersDelta != nil {
			a.activeSum += float64(*iv.Effect.ActivePlayersDelta)
			a.activeN++
		}
	}
	if len(tally) > 0 {
		for _, a := range byType {
			a.agg.Applied, a.agg.Blocked = 0, 0
		}
		for _, t := range tally {
			a := byType[t.Type]
			if a == nil {
				a = &acc{agg: InterventionAggregate{Type: t.Type}}
				byType[t.Type] = a
			}
			a.agg.Applied, a.agg.Blocked = t.Applied, t.Blocked+t.GatedOff
		}
	}
	out := make([]InterventionAggregate, 0, len(byType))
	for _, a := range byType {
		if a.intensityN > 0 {
			m := a.intensitySum / float64(a.intensityN)
			a.agg.MeanIntensityDelta = &m
		}
		if a.activeN > 0 {
			m := a.activeSum / float64(a.activeN)
			a.agg.MeanActiveDelta = &m
		}
		out = append(out, a.agg)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Type < out[j].Type })
	return out
}

// FromPatternRecord builds the per-slot rows of a pattern game (UC-042 BR-006).
// Only the record's slots (the pattern controllers) become rows; the serials the
// record carries are not copied.
func FromPatternRecord(rec patterngame.Record) PatternGame {
	place := make(map[int]int, len(rec.EliminationOrder))
	for i, slot := range rec.EliminationOrder {
		place[slot] = i + 1
	}
	players := make([]PatternPlayer, 0, len(rec.Slots))
	for _, sr := range rec.Slots {
		p := PatternPlayer{
			Slot:             sr.Slot,
			EliminationPlace: place[sr.Slot],
			Won:              rec.WinnerSlot >= 0 && sr.Slot == rec.WinnerSlot,
		}
		if rate := rec.Archetype.StreamRateHz; rate > 0 {
			surv := float64(sr.FramesDelivered) / float64(rate)
			p.SurvivalSeconds = &surv
		}
		players = append(players, p)
	}
	sort.Slice(players, func(i, j int) bool { return players[i].Slot < players[j].Slot })
	return PatternGame{
		Arm:          rec.Arm,
		PairIndex:    rec.PairIndex,
		Archetype:    rec.Archetype.Name,
		ParamsDigest: rec.Archetype.Digest,
		Outcome:      rec.Outcome,
		Censored:     rec.Censored,
		Players:      players,
	}
}

func copyFloat(p *float64) *float64 {
	if p == nil {
		return nil
	}
	v := *p
	return &v
}
