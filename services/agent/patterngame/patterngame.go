// Package patterngame holds the agent side of repeatable pattern-driven shadow
// games (UC-046, #1258): parsing the experiment.mock_pattern_set selection, the
// per-game chaos seed of the rules engine, the flag digest, and a process-wide
// Tracker of live pattern games that the decision loop (remote-backend marking,
// decision recording) and the shadow runner (replay record) share.
//
// The package imports nothing from the agent so both decision/ and gamerunner/
// can depend on it without a cycle.
package patterngame

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"hash/fnv"
	"os"
	"sort"
	"strings"
	"sync"
)

// FlagKey is the agent.json flag selecting parameter sets (flags.md §3.1).
const FlagKey = "experiment.mock_pattern_set"

// SupportedMode is the only game mode pattern games run in v1 (C-021).
const SupportedMode = "JoustFFA"

// ForceEndGraceSeconds is added to a set's max_game_seconds to get the runner's
// force-end backstop (UC-046 A9).
const ForceEndGraceSeconds = 15

// Non-exact reasons (UC-046 BR-008).
const (
	ReasonRemoteBackend  = "remote_backend"
	ReasonFrameOverrun   = "frame_overrun"
	ReasonConcurrentGame = "concurrent_game"
	ReasonNoTermination  = "no_termination"
	ReasonKillSwitch     = "kill_switch"
)

// Experiment discard reasons (UC-046 A2, A4, A13).
const (
	DiscardSetInvalid       = "pattern_set_invalid"
	DiscardSelectionSmall   = "pattern_selection_too_small"
	DiscardModeUnsupported  = "pattern_mode_unsupported"
	selectionNone           = "none"
	chaosSeedDomain         = "agent.rules"
	setIDSeedDomainFallback = 1
)

// ParseSelection splits the flag value into set ids. "", "none" (any case) and
// blank entries yield no ids (today's noise, UC-046 A1). Order is preserved and
// duplicates are kept so the caller can reject them (A4).
func ParseSelection(raw string) []string {
	raw = strings.TrimSpace(raw)
	if raw == "" || strings.EqualFold(raw, selectionNone) {
		return nil
	}
	var ids []string
	for _, part := range strings.Split(raw, ",") {
		if id := strings.TrimSpace(part); id != "" {
			ids = append(ids, id)
		}
	}
	return ids
}

// HasDuplicate reports the first id that appears twice, or "".
func HasDuplicate(ids []string) string {
	seen := make(map[string]struct{}, len(ids))
	for _, id := range ids {
		if _, ok := seen[id]; ok {
			return id
		}
		seen[id] = struct{}{}
	}
	return ""
}

// ChaosSeed derives the rules engine's per-game chaos seed (UC-046 BR-007):
// FNV-1a/64 over the game's rng_seed (8 bytes little-endian), a 0 separator and
// "agent.rules".
func ChaosSeed(rngSeed uint64) uint64 {
	h := fnv.New64a()
	var b [8]byte
	binary.LittleEndian.PutUint64(b[:], rngSeed)
	_, _ = h.Write(b[:])
	_, _ = h.Write([]byte{0})
	_, _ = h.Write([]byte(chaosSeedDomain))
	return h.Sum64()
}

// SetIDSeed is the one-shot runner's default seed when SHADOW_GAME_SEED is unset
// (UC-046 BR-006): FNV-1a/64 of the set id, never 0.
func SetIDSeed(setID string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(setID))
	if s := h.Sum64(); s != 0 {
		return s
	}
	return setIDSeedDomainFallback
}

// FlagDigest is UC-046 BR-011: SHA-256 (lowercase hex) of the bytes of each file
// in order. A missing file contributes its path and "<missing>" so the digest
// still differs from a run where the file existed.
func FlagDigest(paths ...string) string {
	h := sha256.New()
	for _, p := range paths {
		data, err := os.ReadFile(p) // #nosec G304 -- operator-configured flag file paths
		if err != nil {
			_, _ = fmt.Fprintf(h, "%s<missing>", p)
			continue
		}
		_, _ = h.Write(data)
	}
	return hex.EncodeToString(h.Sum(nil))
}

// SetInfo is a resolved parameter set (GetMotionPatternSet answer).
type SetInfo struct {
	ID             string
	Version        int
	SlotCount      int
	StreamRateHz   int
	MaxGameSeconds int
}

// Info is what the runner registers when a pattern game starts.
type Info struct {
	GameID     string
	Set        SetInfo
	Seed       uint64
	Serials    []string // slot order
	FlagDigest string
	// ExperimentID / Arm / PairIndex are empty / -1 for a one-shot game.
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

// DecisionRecord is one rules decision in a pattern game (NFR-042 sequence).
type DecisionRecord struct {
	Intervention string
	TargetSlot   int // -1 when the target is not a player
	Backend      string
}

// Record is the replay record of one finished pattern game (UC-046 step 8).
type Record struct {
	Info
	Completed        bool // ended with a winner (not force-ended / errored)
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

// DecisionSequence renders the decisions as "intervention:slot" (NFR-042).
func (r Record) DecisionSequence() []string {
	out := make([]string, len(r.Decisions))
	for i, d := range r.Decisions {
		out[i] = fmt.Sprintf("%s:%d", d.Intervention, d.TargetSlot)
	}
	return out
}

// EliminationOrder derives the death order from the slots' delivered-frame
// counts: an FFA player stops receiving frames when the coordinator drops it from
// the alive filter, so the slot with the fewest frames died first. The winner (the
// slot with the most frames, only when the game completed) is not part of the
// order. Ties break by slot index. V9 keeps death windows >= 0.5 s apart, far more
// than the filter-update lag, so the order is stable.
func EliminationOrder(slots []SlotRecord, completed bool) (order []int, winner int) {
	sorted := append([]SlotRecord(nil), slots...)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].FramesDelivered != sorted[j].FramesDelivered {
			return sorted[i].FramesDelivered < sorted[j].FramesDelivered
		}
		return sorted[i].Slot < sorted[j].Slot
	})
	winner = -1
	n := len(sorted)
	if completed && n > 0 {
		winner = sorted[n-1].Slot
		sorted = sorted[:n-1]
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
// records. Frame overruns and an incomplete game add their non-exact reasons.
func (t *Tracker) Finish(gameID string, completed bool, slots []SlotRecord) (Record, bool) {
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
	if !completed {
		if _, killed := g.reasons[ReasonKillSwitch]; !killed {
			g.reasons[ReasonNoTermination] = struct{}{}
		}
	}
	order, winner := EliminationOrder(sorted, completed)
	reasons := make([]string, 0, len(g.reasons))
	for r := range g.reasons {
		reasons = append(reasons, r)
	}
	sort.Strings(reasons)
	return Record{
		Info:             g.info,
		Completed:        completed,
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
