package patterngame

import (
	"fmt"
	"sort"
	"sync"
)

// Info is what the runner registers when a pattern game starts.
type Info struct {
	GameID         string
	Archetype      Archetype
	MaxGameSeconds int
	Seed           uint64
	Serials        []string // slot order
	FlagDigest     string
	// ExperimentID / Arm are empty and PairIndex is -1 for a one-shot game.
	ExperimentID string
	Arm          string
	PairIndex    int
	// Abort force-ends the game (kill switch, UC-046 A12); nil = not abortable.
	Abort func() `json:"-"`
}

// SlotRecord is one controller's replay record from RemoveController.
type SlotRecord struct {
	Slot            int
	Serial          string
	FramesDelivered uint64
	SignalHash      uint64
	Checkpoints     []string
	FrameOverruns   uint32
}

// DecisionRecord is one decision in a pattern game (NFR-042 sequence).
type DecisionRecord struct {
	Intervention string
	TargetSlot   int // -1 when the target is not a player
	Backend      string
}

// GameEvents is what the shadow runner saw on the game's event stream (UC-046 BR-014).
type GameEvents struct {
	Deaths []string // serials of player_death events, in order
	Winner string   // serial of the game_winner event, "" when none
	Tie    bool     // a game_tie event was seen
}

// Order sources of Record.OrderSource.
const (
	OrderFromEvents = "events"
	OrderFromFrames = "frames"
)

// Record is the replay record of one finished pattern game (UC-046 step 8).
type Record struct {
	Info
	Outcome          string // OutcomeWinner, OutcomeTie, OutcomeTimeout, OutcomeAgentEnded, OutcomeAborted, OutcomeError
	Censored         bool   // right-censored: at the safety cap (timeout) or by the agent's end_game (agent_ended), BR-013
	OrderSource      string // OrderFromEvents, or OrderFromFrames as the fallback
	Slots            []SlotRecord
	EliminationOrder []int
	WinnerSlot       int // -1 when no winner
	Decisions        []DecisionRecord
	NonExactReasons  []string
}

// Exact reports UC-046 BR-008.
func (r Record) Exact() bool { return len(r.NonExactReasons) == 0 }

// SignalHashes renders the per-slot hashes (slot order) as 16-digit hex.
func (r Record) SignalHashes() []string {
	out := make([]string, len(r.Slots))
	for i, s := range r.Slots {
		out[i] = fmt.Sprintf("%016x", s.SignalHash)
	}
	return out
}

// Checkpoints returns the per-slot checkpoint hashes (slot order).
func (r Record) Checkpoints() [][]string {
	out := make([][]string, len(r.Slots))
	for i, s := range r.Slots {
		out[i] = s.Checkpoints
	}
	return out
}

// DecisionSequence renders the decisions as "intervention:slot" (NFR-042).
func (r Record) DecisionSequence() []string {
	out := make([]string, len(r.Decisions))
	for i, d := range r.Decisions {
		out[i] = fmt.Sprintf("%s:%d", d.Intervention, d.TargetSlot)
	}
	return out
}

// eventOrder maps player_death serials to slots in event order (first death first),
// skipping unknown serials, duplicates and the winner.
func eventOrder(deaths []string, slotOf map[string]int, winner int) ([]int, string) {
	seen := map[int]bool{}
	var order []int
	for _, serial := range deaths {
		slot, ok := slotOf[serial]
		if !ok || seen[slot] || slot == winner {
			continue
		}
		seen[slot] = true
		order = append(order, slot)
	}
	return order, OrderFromEvents
}

// EliminationOrder is the frame-count fallback of UC-046 BR-014. An FFA player stops receiving frames when the
// coordinator drops it from the alive filter, so among the dead the slot with the
// fewest delivered frames died first. The winner comes from the coordinator's
// game_winner event (winnerSlot), not from frame counts: the coordinator ends the
// game on the last death without updating the filter, so the last player to die
// keeps receiving frames as long as the winner. Without a winner (timeout, abort,
// error) every slot within rate frames (one second) of the highest count is a
// survivor. Survivors and the winner are not part of the order. Ties break by slot.
func EliminationOrder(slots []SlotRecord, winnerSlot int, rate int) []int {
	sorted := make([]SlotRecord, 0, len(slots))
	for _, s := range slots {
		if s.Slot != winnerSlot {
			sorted = append(sorted, s)
		}
	}
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].FramesDelivered != sorted[j].FramesDelivered {
			return sorted[i].FramesDelivered < sorted[j].FramesDelivered
		}
		return sorted[i].Slot < sorted[j].Slot
	})
	if winnerSlot < 0 && len(sorted) > 0 {
		top := sorted[len(sorted)-1].FramesDelivered
		cut := len(sorted)
		for cut > 0 && top-sorted[cut-1].FramesDelivered <= uint64(rate) {
			cut--
		}
		sorted = sorted[:cut]
	}
	var order []int
	for _, s := range sorted {
		order = append(order, s.Slot)
	}
	return order
}

type liveGame struct {
	info      Info
	reasons   map[string]struct{}
	decisions []DecisionRecord
	slotOf    map[string]int
}

// Tracker is the registry of live pattern games shared by the decision loops and
// the shadow runner. Safe for concurrent use.
type Tracker struct {
	mu    sync.Mutex
	games map[string]*liveGame
	// concurrent reports whether any game other than gameID is live (set by main
	// from the telemetry multiplexer); nil = unknown, never marks.
	concurrent func(gameID string) bool
}

// NewTracker returns an empty tracker.
func NewTracker() *Tracker { return &Tracker{games: make(map[string]*liveGame)} }

// Default is the process-wide tracker the agent wires into its loops and runners.
var Default = NewTracker()

// SetConcurrencyProbe installs the "is another game live" probe (UC-046 A8).
func (t *Tracker) SetConcurrencyProbe(p func(gameID string) bool) {
	t.mu.Lock()
	t.concurrent = p
	t.mu.Unlock()
}

// Register records a started pattern game. A second live pattern game marks both
// as concurrent (A8).
func (t *Tracker) Register(info Info) {
	slotOf := make(map[string]int, len(info.Serials))
	for i, s := range info.Serials {
		slotOf[s] = i
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	g := &liveGame{info: info, reasons: map[string]struct{}{}, slotOf: slotOf}
	for _, other := range t.games {
		other.reasons[ReasonConcurrentGame] = struct{}{}
		g.reasons[ReasonConcurrentGame] = struct{}{}
	}
	// Only the exact sentinel: an empty digest (tests, hooks without flagd) is not marked.
	if info.FlagDigest == DigestUnavailable {
		g.reasons[ReasonFlagDigestUnavailable] = struct{}{}
	}
	t.games[info.GameID] = g
}

// Active reports whether gameID is a live pattern game.
func (t *Tracker) Active(gameID string) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	_, ok := t.games[gameID]
	return ok
}

// Seed returns the chaos seed of a live pattern game (UC-046 BR-007).
func (t *Tracker) Seed(gameID string) (uint64, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	g, ok := t.games[gameID]
	if !ok {
		return 0, false
	}
	return ChaosSeed(g.info.Seed), true
}

// MarkNonExact adds a non-exact reason to a live pattern game; unknown ids are ignored.
func (t *Tracker) MarkNonExact(gameID, reason string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if g, ok := t.games[gameID]; ok {
		g.reasons[reason] = struct{}{}
	}
}

// RecordDecision appends one decision to a live pattern game's sequence and returns
// the target's slot (-1 when the target is not a player of the game).
func (t *Tracker) RecordDecision(gameID, intervention, targetSerial, backend string) (slot int, ok bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	g, live := t.games[gameID]
	if !live {
		return -1, false
	}
	slot = -1
	if s, found := g.slotOf[targetSerial]; found {
		slot = s
	}
	g.decisions = append(g.decisions, DecisionRecord{Intervention: intervention, TargetSlot: slot, Backend: backend})
	return slot, true
}

// Abort marks a live pattern game non-exact with reason and force-ends it
// through its runner (UC-046 A12). Unknown ids are ignored.
func (t *Tracker) Abort(gameID, reason string) {
	t.mu.Lock()
	g, ok := t.games[gameID]
	var abort func()
	if ok {
		g.reasons[reason] = struct{}{}
		abort = g.info.Abort
	}
	t.mu.Unlock()
	if abort != nil {
		abort()
	}
}

// CheckConcurrency asks the probe whether another game is live and marks the game.
func (t *Tracker) CheckConcurrency(gameID string) {
	t.mu.Lock()
	probe := t.concurrent
	t.mu.Unlock()
	if probe != nil && probe(gameID) {
		t.MarkNonExact(gameID, ReasonConcurrentGame)
	}
}

// Finish removes the game and builds its replay record from the controllers' slot
// records. Frame overruns add their non-exact reason; a timeout alone does not
// (UC-046 BR-008, BR-013).
func (t *Tracker) Finish(gameID, outcome string, events GameEvents, slots []SlotRecord) (Record, bool) {
	t.CheckConcurrency(gameID)
	t.mu.Lock()
	g, ok := t.games[gameID]
	if ok {
		delete(t.games, gameID)
	}
	t.mu.Unlock()
	if !ok {
		return Record{}, false
	}
	sorted := append([]SlotRecord(nil), slots...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Slot < sorted[j].Slot })
	for _, s := range sorted {
		if s.FrameOverruns > 0 {
			g.reasons[ReasonFrameOverrun] = struct{}{}
		}
	}
	if outcome == OutcomeWinner && events.Tie && events.Winner == "" {
		outcome = OutcomeTie
	}
	winner := -1
	if outcome == OutcomeWinner {
		if slot, ok := g.slotOf[events.Winner]; ok {
			winner = slot
		}
	}
	// UC-046 BR-014: the coordinator's player_death events give the order; the
	// delivered-frame counts are only the fallback for a stream that carried none.
	order, source := eventOrder(events.Deaths, g.slotOf, winner)
	if len(order) == 0 {
		order, source = EliminationOrder(sorted, winner, g.info.Archetype.StreamRateHz), OrderFromFrames
	}
	reasons := make([]string, 0, len(g.reasons))
	for r := range g.reasons {
		reasons = append(reasons, r)
	}
	sort.Strings(reasons)
	return Record{
		Info:             g.info,
		Outcome:          outcome,
		Censored:         outcome == OutcomeTimeout || outcome == OutcomeAgentEnded,
		OrderSource:      source,
		Slots:            sorted,
		EliminationOrder: order,
		WinnerSlot:       winner,
		Decisions:        g.decisions,
		NonExactReasons:  reasons,
	}, true
}

// LiveIDs returns the ids of the live pattern games, sorted.
func (t *Tracker) LiveIDs() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	ids := make([]string, 0, len(t.games))
	for id := range t.games {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}

// LoopObserver adapts a Tracker to one game's decision loop (decision.PatternObserver).
type LoopObserver struct {
	Tracker *Tracker
	GameID  string
}

// Active reports whether the loop's game is a live pattern game.
func (o LoopObserver) Active() bool { return o.Tracker.Active(o.GameID) }

// MarkNonExact records a non-exact reason for the loop's game.
func (o LoopObserver) MarkNonExact(reason string) { o.Tracker.MarkNonExact(o.GameID, reason) }

// RecordDecision appends a decision to the loop's game.
func (o LoopObserver) RecordDecision(intervention, targetSerial, backend string) (int, bool) {
	return o.Tracker.RecordDecision(o.GameID, intervention, targetSerial, backend)
}

// LateSeed marks the loop's game non-exact when its rules engine drew chaos rolls
// before it could be seeded (UC-046 BR-007).
func (o LoopObserver) LateSeed() { o.Tracker.MarkNonExact(o.GameID, ReasonUnseededDraws) }

// SeedSource returns the rules engine's chaos seed lookup for the loop's game.
func (o LoopObserver) SeedSource() func() (uint64, bool) {
	return func() (uint64, bool) { return o.Tracker.Seed(o.GameID) }
}
