package infracontext

import (
	"sync"
	"time"

	"go.opentelemetry.io/collector/pdata/pcommon"
)

// Store accumulates InfraContext state from the controller.bluetooth_health span.
// It is safe for concurrent use; all access is guarded by a single mutex.
//
// Invariant (mirrors gamecontext.Store): setters always REPLACE pointer fields
// with fresh pointers and never mutate through an existing pointer. This lets
// Snapshot() shallow-copy each ControllerHealth struct (and thus share the
// pointers) without a later span mutating data already handed out in a snapshot.
type Store struct {
	mu  sync.Mutex
	ctx InfraContext

	// controllerTTL bounds how long a controller is retained after it stops
	// appearing in health spans. Health spans arrive at ~1Hz, so a controller
	// absent for several windows is considered gone.
	controllerTTL time.Duration
	now           func() time.Time

	// ownService is this agent's own OTEL service name. Telemetry from this
	// service is skipped by ApplySpans: the collector fans the agent's own spans
	// back to it (otlp/agent exporter), so the skip is defense-in-depth against a
	// self-ingestion feedback loop. Set once before serving; not mutex-guarded.
	ownService string
}

// NewStore constructs a Store. controllerTTL bounds how long a silent controller
// is retained. now is injectable for tests; nil uses time.Now.
func NewStore(controllerTTL time.Duration, now func() time.Time) *Store {
	if now == nil {
		now = time.Now
	}
	return &Store{
		ctx: InfraContext{
			Controllers: make(map[string]*ControllerHealth),
		},
		controllerTTL: controllerTTL,
		now:           now,
	}
}

// SetOwnService records the agent's own OTEL service name so ApplySpans can skip
// the agent's own telemetry. Must be called before the store starts receiving
// ApplySpans calls.
func (s *Store) SetOwnService(name string) {
	s.ownService = name
}

// isOwnResource reports whether a resource belongs to this agent itself
// (service.name matches ownService) — its telemetry is skipped to break the
// otlp/agent fan-out feedback loop.
func (s *Store) isOwnResource(res pcommon.Resource) bool {
	if s.ownService == "" {
		return false
	}
	v, ok := res.Attributes().Get(resourceAttrServiceName)
	return ok && v.AsString() == s.ownService
}

// controller returns the ControllerHealth for serial, creating it if missing.
// Caller must hold s.mu.
func (s *Store) controller(serial string) *ControllerHealth {
	c := s.ctx.Controllers[serial]
	if c == nil {
		c = &ControllerHealth{Serial: serial}
		s.ctx.Controllers[serial] = c
	}
	return c
}

func ptr[T any](v T) *T { return &v }

// setWindow replaces the window signals wholesale (the producer sends a fresh
// aggregate window each health span). Caller must hold s.mu.
func (s *Store) setWindow(w WindowSignals) {
	s.ctx.Window = w
	s.ctx.UpdatedAt = s.now()
}

// setController applies one per-serial sample, replacing only the present
// pointer fields and stamping LastUpdate. Caller must hold s.mu.
func (s *Store) setController(sample ControllerHealth) {
	c := s.controller(sample.Serial)
	if sample.MovementUpdateHz != nil {
		c.MovementUpdateHz = sample.MovementUpdateHz
	}
	if sample.DroppedEventsPct != nil {
		c.DroppedEventsPct = sample.DroppedEventsPct
	}
	if sample.Adapter != "" {
		c.Adapter = sample.Adapter
	}
	t := s.now()
	c.LastUpdate = t
	s.ctx.UpdatedAt = t
}

// Snapshot returns a deep-enough copy of the context: a fresh Controllers map
// with copied structs. Safe to read after later store mutations because setters
// never mutate through shared pointers.
func (s *Store) Snapshot() InfraContext {
	s.mu.Lock()
	defer s.mu.Unlock()

	out := s.ctx
	out.Controllers = make(map[string]*ControllerHealth, len(s.ctx.Controllers))
	for serial, c := range s.ctx.Controllers {
		cp := *c
		out.Controllers[serial] = &cp
	}
	return out
}

// EvictStale removes controllers that have stopped appearing in health spans
// past the TTL.
func (s *Store) EvictStale() {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	for serial, c := range s.ctx.Controllers {
		if now.Sub(c.LastUpdate) > s.controllerTTL {
			delete(s.ctx.Controllers, serial)
		}
	}
}
