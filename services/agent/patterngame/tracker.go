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

// Record is the replay record of one finished pattern game (UC-046 step 8).
type Record struct {
	Info
	Outcome          string // OutcomeWinner, OutcomeTimeout, OutcomeAborted, OutcomeError
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

// EliminationOrder is UC-046 BR-014. An FFA player stops receiving frames when the
// coordinator drops it from the alive filter, so the slot with the fewest delivered
// frames died first. With a winner, the slot with the most frames is the winner and
// is left out; otherwise every slot within rate frames (one second) of the highest
// count is a survivor and is left out. Ties break by slot index.
func EliminationOrder(slots []SlotRecord, winnerDeclared bool, rate int) (order []int, winner int) {
	sorted := append([]SlotRecord(nil), slots...)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].FramesDelivered != sorted[j].FramesDelivered {
			return sorted[i].FramesDelivered < sorted[j].FramesDelivered
		}
		return sorted[i].Slot < sorted[j].Slot
	})
	winner = -1
	n := len(sorted)
	if n == 0 {
		return nil, winner
	}
	if winnerDeclared {
		winner = sorted[n-1].Slot
		sorted = sorted[:n-1]
	} else {
		top := sorted[n-1].FramesDelivered
		cut := n
		for cut > 0 && top-sorted[cut-1].FramesDelivered <= uint64(rate) {
			cut--
		}
		sorted = sorted[:cut]
	}
	for _, s := range sorted {
		order = append(order, s.Slot)
	}
	return order, winner
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
func (t *Tracker) Finish(gameID, outcome string, slots []SlotRecord) (Record, bool) {
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
	order, winner := EliminationOrder(sorted, outcome == OutcomeWinner, g.info.Archetype.StreamRateHz)
	reasons := make([]string, 0, len(g.reasons))
	for r := range g.reasons {
		reasons = append(reasons, r)
	}
	sort.Strings(reasons)
	return Record{
		Info:             g.info,
		Outcome:          outcome,
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

// SeedSource returns the rules engine's chaos seed lookup for the loop's game.
func (o LoopObserver) SeedSource() func() (uint64, bool) {
	return func() (uint64, bool) { return o.Tracker.Seed(o.GameID) }
}
