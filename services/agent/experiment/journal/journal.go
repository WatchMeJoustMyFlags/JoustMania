package journal

import (
	"fmt"
	"sync"
	"time"
)

// journal.go is the public API (design §9): a small surface mirroring the
// Proposal/gamesummary style. A Journal is the durable, in-memory-backed handle to
// ONE experiment: it owns the immutable intent, the append-only event log, and the
// rolling summary. The in-memory summary is a VIEW; the disk is authoritative.
//
// API:
//   - Create(intent)            → writes intent.json + the initial summary.json.
//   - AppendEvent(event)        → appends to events.jsonl, folds a game_concluded
//                                 fitness into the matching arm's Welford
//                                 accumulator, then atomically rewrites summary.json.
//   - Load(experimentID)        → rehydrate: rebuild the rolling summary from
//                                 intent + events on startup (the #831 fix).
//   - CompactView()             → the bounded {intent, rolling summary, short
//                                 recent-events tail} the LLM/registry consume.

// DefaultTailSize is the number of most-recent events CompactView returns when the
// caller does not specify one. Small and fixed so the compact view stays O(1) in N
// (the bounded-context discipline, §9.2): the prompt carries a short tail, not the
// whole log.
const DefaultTailSize = 10

// statusRunning is the initial status stamped on a freshly created experiment.
const statusRunning = "running"

// Journal is the in-memory handle to one experiment's durable journal. All methods
// are safe for concurrent use — a single mutex serializes the read-modify-write of
// the rolling summary and the event-sequence counter, so concurrent AppendEvent
// calls (e.g. two shadow game-ends, #845) never interleave a fold or reuse a Seq.
type Journal struct {
	store *store

	mu      sync.Mutex
	intent  Intent
	summary Summary
	// tail holds the most recent events for the bounded CompactView. It is capped
	// at maxTail entries (a ring-trimmed slice) so memory stays O(1) in N — the raw
	// log on disk is the unbounded record; the live tail is a small window.
	tail    []Event
	maxTail int
}

// Option configures a Journal at construction.
type Option func(*Journal)

// WithTailSize sets how many recent events the journal keeps in memory for
// CompactView. A non-positive size falls back to DefaultTailSize.
func WithTailSize(n int) Option {
	return func(j *Journal) {
		if n > 0 {
			j.maxTail = n
		}
	}
}

// newJournal builds an empty Journal bound to root, applying options. The store's
// directory is created lazily on the first write.
func newJournal(root string, opts ...Option) *Journal {
	j := &Journal{store: newStore(root), maxTail: DefaultTailSize}
	for _, opt := range opts {
		opt(j)
	}
	return j
}

// Create starts a new experiment: it persists the immutable intent.json and the
// initial summary.json (status "running", one zeroed Welford accumulator per arm).
// The returned Journal is the live handle. Create fails if intent.json already
// exists for this experiment_id — the intent is immutable and write-once, so
// re-creating would risk silently clobbering an in-flight experiment; the caller
// should Load an existing one instead.
//
// Single-writer-per-experiment: a Journal serializes its own writes with a mutex,
// but that does NOT coordinate across two handles to the SAME experiment dir.
// Callers MUST hold at most one live Journal per experiment dir at a time (two
// handles would interleave appends and racily rewrite summary.json). The registry
// (#978) owns this single-handle invariant.
func Create(root string, intent Intent, opts ...Option) (*Journal, error) {
	if intent.ExperimentID == "" {
		return nil, fmt.Errorf("journal: intent.ExperimentID is required")
	}
	if intent.SchemaVersion == 0 {
		intent.SchemaVersion = SchemaVersion
	}

	j := newJournal(root, opts...)
	if _, err := j.store.readIntent(intent.ExperimentID); err == nil {
		return nil, fmt.Errorf("journal: experiment %q already exists", intent.ExperimentID)
	}

	j.intent = intent
	j.summary = Summary{
		SchemaVersion: SchemaVersion,
		ExperimentID:  intent.ExperimentID,
		Status:        statusRunning,
		UpdatedAt:     intent.CreatedAt,
		Arms:          make(map[string]*ArmStat, len(intent.Arms)),
	}
	for _, arm := range intent.Arms {
		j.summary.Arms[arm] = &ArmStat{}
	}

	intentPath := j.store.dir(intent.ExperimentID) + "/" + intentFile
	if err := writeJSONAtomic(intentPath, j.intent); err != nil {
		return nil, fmt.Errorf("journal: write intent: %w", err)
	}
	if err := j.writeSummary(); err != nil {
		return nil, fmt.Errorf("journal: write initial summary: %w", err)
	}
	return j, nil
}

// AppendEvent appends one event to the durable log and updates the rolling state.
// It is the single write path for progression:
//  1. assign the next monotonic Seq,
//  2. append the event to events.jsonl (append-only, fsynced),
//  3. for a game_concluded with a fitness sample, fold it into the matching arm's
//     Welford accumulator (O(1)),
//  4. carry a Verdict / Status onto the summary if the event supplies one,
//  5. atomically rewrite summary.json.
//
// The event is durably logged BEFORE the summary is rewritten, so a crash between
// the two leaves the log ahead of the summary — Load then rebuilds the summary
// from the log and is correct again. The reverse order could persist a summary
// counting an event that was never logged. The caller's e.Seq is ignored and
// overwritten with the journal's counter.
func (j *Journal) AppendEvent(e Event) error {
	j.mu.Lock()
	defer j.mu.Unlock()

	e.Seq = j.summary.EventCount + 1

	path := j.store.dir(j.intent.ExperimentID) + "/" + eventsFile
	if err := appendEventLine(path, e); err != nil {
		return fmt.Errorf("journal: append event: %w", err)
	}

	j.apply(e)

	if err := j.writeSummary(); err != nil {
		return fmt.Errorf("journal: rewrite summary: %w", err)
	}
	return nil
}

// apply folds one event into the in-memory rolling summary and tail. It is the
// PURE state transition shared by AppendEvent (live) and Load (replay), so the
// rehydrated summary is byte-for-byte the same as the one built incrementally —
// the durability guarantee (§9.3). It assumes j.mu is held.
func (j *Journal) apply(e Event) {
	j.summary.EventCount = e.Seq
	if !e.At.IsZero() {
		j.summary.UpdatedAt = e.At
	}

	switch e.Kind {
	case KindGameConcluded:
		if e.Fitness != nil {
			if arm := j.summary.Arms[e.Arm]; arm != nil {
				arm.Add(*e.Fitness)
			}
			// An event for an arm not in Intent.Arms is still recorded in the log
			// (the complete audit trail) but folds into no accumulator.

			// Common-Random-Numbers paired fold (#1004): if this conclusion carries a
			// pair index, drive the pending-pairs buffer toward a per-pair difference.
			j.applyPairConcluded(e)
		}
	case KindGameReleased:
		// A non-counting release (#1014) of a game that never produced a usable
		// fitness sample. If it was the first-concluded half of a still-open pair, drop
		// the dangling half so it can NEVER be folded as a fabricated difference — the
		// partner is gone (the experiment is tearing down or the game errored), so the
		// pair can never complete (#1004 carried-over item 1). A released game that was
		// not a parked half (its partner already folded, or it never parked) is a no-op.
		j.applyPairReleased(e)
	case KindInterimVerdict, KindOutcome:
		if e.Verdict != nil {
			v := *e.Verdict
			j.summary.Verdict = &v
		}
		if e.Status != "" {
			j.summary.Status = e.Status
		}
	case KindDecision:
		if e.Status != "" {
			j.summary.Status = e.Status
		}
	}

	j.appendTail(e)
}

// applyPairConcluded folds a concluded game into the Common-Random-Numbers
// pending-pairs buffer (#1003/#1004). It is called only for a KindGameConcluded
// event that carries a Fitness sample. Assumes j.mu is held (called from apply).
//
// A pair's two arms conclude at DIFFERENT times (under the single-game coordinator
// they are even spawned on different ticks), so:
//   - the FIRST arm of a pair to conclude is PARKED in PendingPairs[pairIndex];
//   - when the SECOND (partner) arm concludes, the per-pair difference
//     d = f_experimental − f_control is computed and folded into PairDiff, and the
//     pending entry is dropped (the buffer drains).
//
// Folding requires a genuine experimental↔control pair: a second conclusion for the
// SAME arm at the same pair index (which cannot happen for a well-formed pair, but
// is handled defensively) overwrites the parked half rather than folding a
// same-arm difference. A conclusion without a pair index (legacy/unpaired) is
// ignored here — the per-arm Welford fold above still captures it for the two-arm
// fallback verdict.
func (j *Journal) applyPairConcluded(e Event) {
	if e.PairIndex == nil {
		return // not a paired game; the two-arm path covers it.
	}
	pi := *e.PairIndex
	f := *e.Fitness

	parked, pending := j.summary.PendingPairs[pi]
	if !pending {
		// First half of this pair to conclude — park it (lazily create the buffer).
		if j.summary.PendingPairs == nil {
			j.summary.PendingPairs = make(map[int]PendingHalf)
		}
		j.summary.PendingPairs[pi] = PendingHalf{Arm: e.Arm, Fitness: f}
		return
	}

	// The partner concluded. Fold a per-pair difference only if the two halves are
	// the canonical experimental/control arms (one of each). A same-arm repeat is a
	// malformed pair — refresh the parked half and do not fold a bogus difference.
	d, ok := pairDelta(parked.Arm, parked.Fitness, e.Arm, f)
	if !ok {
		j.summary.PendingPairs[pi] = PendingHalf{Arm: e.Arm, Fitness: f}
		return
	}

	if j.summary.PairDiff == nil {
		j.summary.PairDiff = &Welford{}
	}
	j.summary.PairDiff.Add(d)
	delete(j.summary.PendingPairs, pi)
	if len(j.summary.PendingPairs) == 0 {
		// Keep the map nil when empty so a drained buffer serializes away (omitempty).
		j.summary.PendingPairs = nil
	}
}

// applyPairReleased drops a parked pending half whose game was released without a
// usable fitness sample (#1014/#1004). A released game that was the FIRST-concluded
// half of an open pair can never complete (its partner is gone), so its half must be
// dropped — never folded as a fabricated difference (#1004 carried-over item 1).
// Assumes j.mu is held.
func (j *Journal) applyPairReleased(e Event) {
	if e.PairIndex == nil || j.summary.PendingPairs == nil {
		return
	}
	pi := *e.PairIndex
	if parked, ok := j.summary.PendingPairs[pi]; ok && parked.Arm == e.Arm {
		// Only drop the half that THIS released game parked (arm match). A release for
		// a pair whose parked half belongs to the OTHER arm is left intact: the parked
		// arm still concluded normally and may yet match a re-spawned partner.
		delete(j.summary.PendingPairs, pi)
		if len(j.summary.PendingPairs) == 0 {
			j.summary.PendingPairs = nil
		}
	}
}

// pairDelta returns the signed per-pair difference d = f_experimental − f_control
// for the two concluded halves of a Common-Random-Numbers pair, and whether the
// two arms form a canonical experimental/control pair (one of each). It returns
// ok=false when the two halves are the same arm or either is a non-canonical arm —
// in which case no PairDiff is folded and the verdict falls back to the two-arm
// path.
func pairDelta(armA string, fA float64, armB string, fB float64) (d float64, ok bool) {
	switch {
	case armA == ArmExperimental && armB == ArmControl:
		return fA - fB, true
	case armA == ArmControl && armB == ArmExperimental:
		return fB - fA, true
	default:
		return 0, false
	}
}

// appendTail adds e to the bounded recent-events tail, trimming the oldest so the
// tail never exceeds maxTail entries — keeping CompactView O(1) in N.
func (j *Journal) appendTail(e Event) {
	j.tail = append(j.tail, e)
	if len(j.tail) > j.maxTail {
		// Drop the oldest entries; copy into a fresh slice so the backing array of
		// the trimmed-off events can be reclaimed (no unbounded growth via reslicing).
		trimmed := make([]Event, j.maxTail)
		copy(trimmed, j.tail[len(j.tail)-j.maxTail:])
		j.tail = trimmed
	}
}

// writeSummary atomically rewrites summary.json from the in-memory summary. Assumes
// j.mu is held.
func (j *Journal) writeSummary() error {
	path := j.store.dir(j.intent.ExperimentID) + "/" + summaryFile
	return writeJSONAtomic(path, j.summary)
}

// Load (rehydrate) rebuilds a live Journal for an existing experiment from disk:
// it reads the immutable intent, seeds an empty summary with one Welford
// accumulator per arm, then replays the append-only event log through the SAME
// apply transition AppendEvent uses — so the rebuilt rolling summary equals the
// pre-restart one. This is the #831 in-memory-loss fix: the in-memory state is a
// VIEW of the durable journal, reconstructed on startup. summary.json on disk is a
// cache/telemetry view; the log is the source of truth, so Load does NOT trust it
// and recomputes from events. A torn final event line (a partial append left by a
// crash) is dropped and truncated by the store so Load still reconverges — see
// store.readEvents; an interior malformed line stays a hard error.
//
// Single-writer-per-experiment: like Create, the returned Journal does not
// coordinate with other handles to the same experiment dir. Callers MUST keep at
// most one live Journal per experiment dir; the registry (#978) owns this.
func Load(root string, experimentID string, opts ...Option) (*Journal, error) {
	j := newJournal(root, opts...)

	intent, err := j.store.readIntent(experimentID)
	if err != nil {
		return nil, fmt.Errorf("journal: load intent: %w", err)
	}
	j.intent = intent

	j.summary = Summary{
		SchemaVersion: SchemaVersion,
		ExperimentID:  intent.ExperimentID,
		Status:        statusRunning,
		UpdatedAt:     intent.CreatedAt,
		Arms:          make(map[string]*ArmStat, len(intent.Arms)),
	}
	for _, arm := range intent.Arms {
		j.summary.Arms[arm] = &ArmStat{}
	}

	events, err := j.store.readEvents(experimentID)
	if err != nil {
		return nil, fmt.Errorf("journal: load events: %w", err)
	}
	for _, e := range events {
		j.apply(e)
	}
	return j, nil
}

// CompactView is the bounded read accessor the LLM and the live registry consume
// (design §9.2). It returns the immutable intent, a COPY of the rolling summary
// (per-arm count/mean/variance + current verdict), and a short tail of the most
// recent events — NEVER the full game-by-game log. Its size is O(arms + tailSize),
// constant in N, so a prompt built from it stays roughly the same size whether the
// experiment has seen 5 games or 500. The returned values are copies, safe for the
// caller to read without holding the journal lock.
func (j *Journal) CompactView() CompactView {
	j.mu.Lock()
	defer j.mu.Unlock()

	armsCopy := make(map[string]ArmView, len(j.summary.Arms))
	for name, stat := range j.summary.Arms {
		armsCopy[name] = ArmView{
			Count:    stat.Count,
			Mean:     stat.Mean,
			Variance: stat.Variance(),
		}
	}

	var verdict *Verdict
	if j.summary.Verdict != nil {
		v := *j.summary.Verdict
		verdict = &v
	}

	tail := make([]Event, len(j.tail))
	copy(tail, j.tail)

	return CompactView{
		Intent:     j.intent,
		Status:     j.summary.Status,
		UpdatedAt:  j.summary.UpdatedAt,
		EventCount: j.summary.EventCount,
		Arms:       armsCopy,
		Verdict:    verdict,
		RecentTail: tail,
	}
}

// ArmView is the per-arm aggregate exposed in a CompactView: the derived
// sufficient statistics (variance already computed from the Welford M2), without
// the internal M2 the LLM does not need.
//
// NOTE for a downstream significance test (#979): Variance here is the POPULATION
// variance (M2/count) and would BIAS small-N comparisons. An unbiased / Welch
// estimator MUST instead consume the raw M2 (+ count) from Summary() — see
// Journal.Summary(), which exposes the full Welford state including M2. CompactView
// is the bounded LLM/registry view, not the statistics input.
type ArmView struct {
	Count    int     `json:"count"`
	Mean     float64 `json:"mean_fitness"`
	Variance float64 `json:"variance"`
}

// CompactView is the bounded snapshot fed to the LLM / registry (§9.2). Its size is
// O(arms + len(RecentTail)) — independent of how many games the experiment has run.
type CompactView struct {
	// Intent is the immutable goal.
	Intent Intent `json:"intent"`
	// Status is the experiment's lifecycle status.
	Status string `json:"status"`
	// UpdatedAt is the timestamp of the last folded event.
	UpdatedAt time.Time `json:"updated_at"`
	// EventCount is how many events have been logged (the full-log size, for context
	// — the log itself is NOT included).
	EventCount int `json:"event_count"`
	// Arms is the per-arm rolling aggregates.
	Arms map[string]ArmView `json:"arms"`
	// Verdict is the current rolling verdict, or nil.
	Verdict *Verdict `json:"verdict,omitempty"`
	// RecentTail is the short bounded tail of most-recent events (oldest-first).
	RecentTail []Event `json:"recent_tail"`
}

// Intent returns a copy of the experiment's immutable intent.
func (j *Journal) Intent() Intent {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.intent
}

// Events returns the FULL ordered append-only event log read from disk — the
// complete game-by-game audit trail, NOT the bounded in-memory tail CompactView
// exposes. It is a READ-ONLY accessor for offline / dossier consumers (#1183) that
// need every per-game conclusion, not the O(1) rolling aggregate. It re-reads
// events.jsonl from disk on each call (the live journal only retains the recent
// tail in memory, §9.2), so it is O(N) and intended for a human-driven dossier
// read, not a hot path.
//
// It is strictly READ-ONLY: it uses readEventsReadOnly, which NEVER truncates the
// file. A dossier consumer opens an INDEPENDENT handle to a possibly-LIVE
// experiment's dir while the registry's writing handle holds it, so it must not
// invoke the truncating torn-tail recovery (that could race an in-flight append and
// discard an event the live writer just committed, breaking the §9.1b append-only
// guarantee and the single-writer invariant). A genuine torn tail is dropped
// in-memory here and recovered by the writing handle on its next Load/AppendEvent.
// The returned slice is freshly allocated; mutating it cannot affect journal state.
func (j *Journal) Events() ([]Event, error) {
	j.mu.Lock()
	id := j.intent.ExperimentID
	j.mu.Unlock()
	return j.store.readEventsReadOnly(id)
}

// Summary returns a deep copy of the current rolling summary (the full per-arm
// Welford state, including M2). The map and arm pointers are copied so the caller
// cannot mutate the journal's live state.
func (j *Journal) Summary() Summary {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.copySummaryLocked()
}

// copySummaryLocked deep-copies the rolling summary. Assumes j.mu is held.
func (j *Journal) copySummaryLocked() Summary {
	out := j.summary
	// Objective is a derived VIEW field (#992): always sourced from the immutable
	// intent, never folded onto the persisted summary, so it survives a rehydrate
	// (intent.json is authoritative) without summary-schema migration.
	out.Objective = j.intent.Objective
	out.Arms = make(map[string]*ArmStat, len(j.summary.Arms))
	for name, stat := range j.summary.Arms {
		s := *stat
		out.Arms[name] = &s
	}
	if j.summary.Verdict != nil {
		v := *j.summary.Verdict
		out.Verdict = &v
	}
	if j.summary.PairDiff != nil {
		pd := *j.summary.PairDiff
		out.PairDiff = &pd
	}
	if len(j.summary.PendingPairs) > 0 {
		out.PendingPairs = make(map[int]PendingHalf, len(j.summary.PendingPairs))
		for k, v := range j.summary.PendingPairs {
			out.PendingPairs[k] = v
		}
	}
	return out
}
