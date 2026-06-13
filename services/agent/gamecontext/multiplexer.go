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
}

// NewMultiplexer builds a Multiplexer over the given partition factory. The
// factory is responsible for applying lifecycle parameters and wiring OnGameEnd;
// see main.go for the production wiring.
func NewMultiplexer(newStore func(gameID string) *Store) *Multiplexer {
	return &Multiplexer{
		stores:   make(map[string]*Store),
		newStore: newStore,
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
	for i := 0; i < dps.Len(); i++ {
		dp := dps.At(i)
		gameID := gameIDOf(dp.Attributes())
		s := m.store(gameID)
		if s.applyDataPoint(name, dp) {
			touched.add(gameID)
		}
	}
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
func (m *Multiplexer) EvictStale() {
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
		return
	}

	// Re-check under the write lock so a concurrent Apply that recreated/refreshed
	// the partition between the read above and now is not clobbered. Only drop the
	// exact pointer we evaluated and only if it is still drained.
	m.mu.Lock()
	for _, id := range removable {
		s := m.stores[id]
		if s != nil && s == stores[id] && s.isDrained() {
			delete(m.stores, id)
		}
	}
	m.mu.Unlock()
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
