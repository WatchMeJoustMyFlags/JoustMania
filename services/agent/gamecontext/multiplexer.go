package gamecontext

import (
	"sync"

	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/pmetric"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// FallbackGameID is the partition key used for signals that carry no game_id
// label. It is the zero-regression linchpin (#845 PR B): in single-game mode, or
// against a coordinator without the #845/#853 game_id labels, every signal lands
// here and the Multiplexer behaves byte-for-byte like today's single Store. The
// fallback partition is created lazily and is NEVER wholesale-removed by eviction
// (its session still resets on grace exactly as a single Store's would).
const FallbackGameID = ""

// Multiplexer partitions accumulated game context by game_id (#845 PR B). Each
// partition is a full gamecontext.Store; per-datapoint and per-span dispatch
// selects the partition on the signal's resolved gameLabels.GameID BEFORE
// applying the signal, so two concurrent games never bleed players, sessions, or
// elimination order into one another.
//
// The single-store invariants are preserved within each partition. The
// Multiplexer adds partition selection (routing) and partition lifecycle
// (lazy creation + stale removal) on top.
type Multiplexer struct {
	mu     sync.RWMutex
	stores map[string]*Store

	// newStore is the partition factory. It applies the lifecycle parameters
	// (playerTTL / sessionGrace / now) AND wires per-partition OnGameEnd, so the
	// post-game analyst fires per game on the right partition's state. gameID is
	// passed so a factory may key per-partition behavior on it; the fallback
	// partition is created with FallbackGameID.
	newStore func(gameID string) *Store

	// ownService is the agent's own OTEL service name, applied to every partition
	// AND used by the Multiplexer's own per-resource self-skip so an own-resource
	// batch is never routed to any partition. Set once before serving.
	ownService string

	// names is the serial -> display-name view from controller_info{serial,name}
	// (#1295). controller-manager metrics carry no game_id, so the view is
	// process-wide rather than per partition; it is bounded by the number of
	// controllers ever seen and guarded by its own lock.
	namesMu sync.RWMutex
	names   map[string]string

	// ended remembers the ids of games that ended in a partition since removed
	// (#1494, UC-041 BR-005). Game ids are never reused, so when signals for such
	// an id arrive again (a game_active series resuming after a stale end, a late
	// batch) the recreated partition starts latched and never runs the game-end
	// chain a second time. Guarded by mu; bounded to endedGamesCap ids.
	ended endedGames
}

// endedGamesCap bounds Multiplexer.ended: far more games than can end within
// any realistic late-delivery window, and a few KiB of memory.
const endedGamesCap = 512

// endedGames is a bounded FIFO set of ended game ids. Not concurrency-safe.
type endedGames struct {
	set   map[string]struct{}
	order []string
}

func (e *endedGames) add(id string) {
	if e.set == nil {
		e.set = make(map[string]struct{})
	}
	if _, ok := e.set[id]; ok {
		return
	}
	if len(e.order) >= endedGamesCap {
		delete(e.set, e.order[0])
		e.order = e.order[1:]
	}
	e.set[id] = struct{}{}
	e.order = append(e.order, id)
}

func (e *endedGames) has(id string) bool {
	_, ok := e.set[id]
	return ok
}

// NewMultiplexer builds a Multiplexer over the given partition factory. The
// factory is responsible for applying lifecycle parameters and wiring OnGameEnd;
// see main.go for the production wiring.
func NewMultiplexer(newStore func(gameID string) *Store) *Multiplexer {
	return &Multiplexer{
		stores:   make(map[string]*Store),
		newStore: newStore,
		names:    make(map[string]string),
	}
}

// SetOwnService records the agent's own OTEL service name so own-telemetry is
// skipped before routing. The factory should also call Store.SetOwnService on
// each partition (defense in depth); the Multiplexer's own-resource skip short
// circuits before any partition is selected. Must be called before serving.
func (m *Multiplexer) SetOwnService(name string) {
	m.mu.Lock()
	m.ownService = name
	m.mu.Unlock()
}

// store returns the Store for gameID, creating it lazily via the factory. Safe
// for concurrent use: a read-locked fast path is followed by a write-locked
// double-checked create.
func (m *Multiplexer) store(gameID string) *Store {
	m.mu.RLock()
	s := m.stores[gameID]
	m.mu.RUnlock()
	if s != nil {
		return s
	}

	m.mu.Lock()
	defer m.mu.Unlock()
	if s = m.stores[gameID]; s != nil {
		return s
	}
	s = m.newStore(gameID)
	if gameID != FallbackGameID {
		s.setPartition(m.ended.has(gameID))
	}
	m.stores[gameID] = s
	return s
}

// ApplyMetrics walks the metric batch, routing each datapoint to its game_id
// partition (FallbackGameID for unlabeled datapoints) before applying it. It
// returns the deduped set of game_ids that were touched, so the caller can
// snapshot + gate + evaluate exactly those partitions.
func (m *Multiplexer) ApplyMetrics(md pmetric.Metrics) []string {
	touched := newTouchedSet()
	rms := md.ResourceMetrics()
	for i := 0; i < rms.Len(); i++ {
		rm := rms.At(i)
		if isOwnResource(rm.Resource(), m.ownServiceName()) {
			continue
		}
		sms := rm.ScopeMetrics()
		for j := 0; j < sms.Len(); j++ {
			ms := sms.At(j).Metrics()
			for k := 0; k < ms.Len(); k++ {
				m.applyMetric(ms.At(k), touched)
			}
		}
	}
	return touched.ids()
}

// applyMetric routes each of a metric's datapoints to its partition. The game_id
// is resolved PER DATAPOINT (a single metric may carry datapoints for multiple
// concurrent games), so the partition is selected inside the datapoint loop.
func (m *Multiplexer) applyMetric(metric pmetric.Metric, touched *touchedSet) {
	name := metric.Name()
	dps := dataPoints(metric)
	if name == metricControllerInfo {
		// Identity only (#1295): never routed to a partition, never a trigger.
		m.recordPlayerNames(dps)
		return
	}
	for i := 0; i < dps.Len(); i++ {
		dp := dps.At(i)
		gameID := gameIDOf(dp.Attributes())
		s := m.store(gameID)
		if s.applyDataPoint(name, dp) {
			touched.add(gameID)
		}
	}
}

// recordPlayerNames stores the name label of each controller_info datapoint
// under its serial label. Datapoints without both labels are ignored.
func (m *Multiplexer) recordPlayerNames(dps pmetric.NumberDataPointSlice) {
	for i := 0; i < dps.Len(); i++ {
		attrs := dps.At(i).Attributes()
		serial, okSerial := attrs.Get(attrSerial)
		name, okName := attrs.Get(attrName)
		if !okSerial || !okName || serial.AsString() == "" || name.AsString() == "" {
			continue
		}
		m.namesMu.Lock()
		m.names[serial.AsString()] = name.AsString()
		m.namesMu.Unlock()
	}
}

// PlayerName returns the display name the controller manager reported for
// serial on controller_info (#1295), or "" when none was seen. Safe for
// concurrent use; the decision loops use it for the player.name span attribute.
func (m *Multiplexer) PlayerName(serial string) string {
	m.namesMu.RLock()
	defer m.namesMu.RUnlock()
	return m.names[serial]
}

// ApplySpans walks the trace batch, routing each span to its game_id partition
// (FallbackGameID for unlabeled spans) before applying it. It returns the deduped
// set of game_ids touched.
func (m *Multiplexer) ApplySpans(td ptrace.Traces) []string {
	touched := newTouchedSet()
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
		rs := rss.At(i)
		if isOwnResource(rs.Resource(), m.ownServiceName()) {
			continue
		}
		sss := rs.ScopeSpans()
		for j := 0; j < sss.Len(); j++ {
			spans := sss.At(j).Spans()
			for k := 0; k < spans.Len(); k++ {
				span := spans.At(k)
				if ignoredSpan(span) {
					continue // never creates a partition (game.shield_charge, #1837)
				}
				gameID := spanGameIDOf(span.Attributes())
				s := m.store(gameID)
				if s.applySpan(span) {
					touched.add(gameID)
				}
			}
		}
	}
	return touched.ids()
}

// Snapshot returns a deep-enough snapshot of the partition for gameID and whether
// that partition exists. A non-existent partition returns the zero GameContext and
// false — callers that race against EvictStale removing a partition simply finish
// against their captured snapshot (or skip when ok is false).
func (m *Multiplexer) Snapshot(gameID string) (GameContext, bool) {
	m.mu.RLock()
	s := m.stores[gameID]
	m.mu.RUnlock()
	if s == nil {
		return GameContext{}, false
	}
	return s.Snapshot(), true
}

// AppendInterventionEvent routes a dispatched/blocked intervention to the
// game_id partition's Store and records it in that partition's #916 rolling
// narrative timeline (#945). It is the receiver-side half of the decision-Loop
// seam: main.go's loopFactory hands each per-game Loop a recorder closure over
// (this Multiplexer, that loop's game_id), so when runDecision decides an
// intervention's outcome the event lands in the timeline of the game it targeted
// and nowhere else.
//
// PER-GAME ISOLATION (#845): the partition is selected by game_id with the SAME
// lazy store() routing every signal uses — an unlabeled game_id (FallbackGameID)
// lands in the fallback partition, exactly like an unlabeled signal — so two
// concurrent games keep independent intervention histories. The partition is
// created on demand if it does not yet exist (a decision could in principle reach
// the seam before any signal created the partition; routing it the same way keeps
// the event with its game rather than dropping it).
//
// Concurrency: store() is mutex-guarded for lazy create, and Store.
// AppendInterventionEvent is guarded by the Store's own mutex (#916), so this is
// race-safe under the concurrent Export-handler goroutines that drive runDecision.
func (m *Multiplexer) AppendInterventionEvent(gameID, intervention, serial string, blocked bool) {
	m.store(gameID).AppendInterventionEvent(intervention, serial, blocked)
}

// AppendInterventionInPlace routes an already-applied intervention (UC-040 A35) to
// the game_id partition's narrative timeline, as AppendInterventionEvent does.
func (m *Multiplexer) AppendInterventionInPlace(gameID, intervention, serial string) {
	m.store(gameID).AppendInterventionInPlace(intervention, serial)
}

// RecordInterventionApply routes a dispatched intervention's write outcome to the
// game_id partition's tally (#1771), with the same routing as
// AppendInterventionEvent.
func (m *Multiplexer) RecordInterventionApply(gameID, intervention string, outcome ApplyOutcome) {
	m.store(gameID).RecordInterventionApply(intervention, outcome)
}

// Partitions returns the current set of partition game_ids (including
// FallbackGameID when present). Order is unspecified.
func (m *Multiplexer) Partitions() []string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	ids := make([]string, 0, len(m.stores))
	for id := range m.stores {
		ids = append(ids, id)
	}
	return ids
}

// EvictStale runs each partition's own per-store eviction (TTL players + session
// grace reset), then drops any non-fallback partition whose session has ended past
// grace AND retains no fresh players — its Store is removed from the map entirely.
//
// The fallback partition is NEVER removed wholesale: its session still resets on
// grace via the per-store EvictStale above, exactly as today's single Store.
//
// Removal-safety: the per-store eviction work runs while holding only the
// individual Store's mutex (not m.mu); the map structural change is the only thing
// done under m.mu's write lock, and only after capturing the partition pointers
// under a read lock. An evaluate already holding a removed partition's snapshot
// finishes against that snapshot; if signals for the removed game resume, store()
// lazily recreates the partition.
//
// It returns the ids of the partitions it removed. A game's per-player
// intervention values need no agent-side pruning: the flag service drops them
// when the game ends (UC-040 BR-016).
func (m *Multiplexer) EvictStale() []string {
	// Snapshot the partition pointers under RLock, then release before the
	// per-store eviction (long work) so Apply*/Snapshot are not blocked.
	m.mu.RLock()
	stores := make(map[string]*Store, len(m.stores))
	for id, s := range m.stores {
		stores[id] = s
	}
	m.mu.RUnlock()

	var removable []string
	for id, s := range stores {
		s.EvictStale()
		if id == FallbackGameID {
			continue // fallback partition is never removed wholesale
		}
		if s.isDrained() {
			removable = append(removable, id)
		}
	}

	if len(removable) == 0 {
		return nil
	}

	// Re-check under the write lock so a concurrent Apply that recreated/refreshed
	// the partition between the read above and now is not clobbered. Only drop the
	// exact pointer we evaluated and only if it is still drained.
	var removed []string
	m.mu.Lock()
	for _, id := range removable {
		s := m.stores[id]
		if s != nil && s == stores[id] && s.isDrained() {
			if s.hasEnded() {
				m.ended.add(id)
			}
			delete(m.stores, id)
			removed = append(removed, id)
		}
	}
	m.mu.Unlock()
	return removed
}

// ownServiceName reads ownService under the read lock.
func (m *Multiplexer) ownServiceName() string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.ownService
}

// touchedSet collects the deduped game_ids touched by an Apply pass, preserving
// first-seen order so callers (and tests) get a stable result.
type touchedSet struct {
	seen  map[string]struct{}
	order []string
}

func newTouchedSet() *touchedSet {
	return &touchedSet{seen: make(map[string]struct{})}
}

func (t *touchedSet) add(id string) {
	if _, ok := t.seen[id]; ok {
		return
	}
	t.seen[id] = struct{}{}
	t.order = append(t.order, id)
}

func (t *touchedSet) ids() []string { return t.order }

// isOwnResource reports whether res belongs to the agent's own service. A blank
// ownService never matches (identical to a fresh Store before SetOwnService).
func isOwnResource(res pcommon.Resource, ownService string) bool {
	if ownService == "" {
		return false
	}
	v, ok := res.Attributes().Get(resourceAttrServiceName)
	return ok && v.AsString() == ownService
}
