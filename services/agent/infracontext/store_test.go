package infracontext

import (
	"sync"
	"testing"
	"time"
)

// clock is a mutable injected time source for deterministic tests.
type clock struct {
	mu sync.Mutex
	t  time.Time
}

func (c *clock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *clock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func TestStore_SnapshotIsolation(t *testing.T) {
	s := newTestStore()
	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 10},
		"rust", []sampleEvent{{serial: "A", hz: 60, adapter: "python"}}, false))
	snap := s.Snapshot()

	// Mutate the store after snapshotting (new window + new controller signal).
	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 99},
		"unstable", []sampleEvent{{serial: "A", hz: 5, adapter: "rust"}}, false))

	if snap.Window.EventGapMs == nil || *snap.Window.EventGapMs != 10 {
		t.Fatalf("snapshot window mutated: got %v, want 10", snap.Window.EventGapMs)
	}
	if snap.Window.TargetBackend != "rust" {
		t.Fatalf("snapshot target mutated: got %q, want rust", snap.Window.TargetBackend)
	}
	a := snap.Controllers["A"]
	if a == nil || a.MovementUpdateHz == nil || *a.MovementUpdateHz != 60 || a.Adapter != "python" {
		t.Fatalf("snapshot controller mutated: %+v, want hz=60 adapter=python", a)
	}
}

func TestStore_WindowReplacedPerSpan(t *testing.T) {
	s := newTestStore()
	// First span sets dropped_events_pct; second span omits it entirely.
	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{
		AttrEventGapMs:       10,
		AttrDroppedEventsPct: 0.5,
	}, "", nil, false))
	if got := s.Snapshot().Window.DroppedEventsPct; got == nil || *got != 0.5 {
		t.Fatalf("dropped_events_pct = %v, want 0.5", got)
	}

	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{
		AttrEventGapMs: 20,
	}, "", nil, false))
	snap := s.Snapshot()
	if snap.Window.EventGapMs == nil || *snap.Window.EventGapMs != 20 {
		t.Fatalf("event_gap_ms = %v, want 20 (replaced)", snap.Window.EventGapMs)
	}
	// Window is replaced wholesale, so the absent attr drops back to nil.
	if snap.Window.DroppedEventsPct != nil {
		t.Fatalf("dropped_events_pct = %v, want nil (window replaced, attr absent)", snap.Window.DroppedEventsPct)
	}
}

func TestStore_EvictStaleControllers(t *testing.T) {
	clk := &clock{t: time.Unix(0, 0)}
	s := NewStore(5*time.Second, clk.now)

	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 10},
		"", []sampleEvent{
			{serial: "A", hz: 60, adapter: "python"},
			{serial: "B", hz: 55, adapter: "rust"},
		}, false))

	// A keeps reporting; B goes silent.
	clk.advance(3 * time.Second)
	s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 11},
		"", []sampleEvent{{serial: "A", hz: 60, adapter: "python"}}, false))

	clk.advance(3 * time.Second) // B now 6s silent (> 5s TTL), A only 3s
	s.EvictStale()

	snap := s.Snapshot()
	if _, ok := snap.Controllers["A"]; !ok {
		t.Fatal("fresh controller A must be retained")
	}
	if _, ok := snap.Controllers["B"]; ok {
		t.Fatal("silent controller B must be evicted past TTL")
	}
}

func TestStore_ConcurrencySmoke(t *testing.T) {
	s := NewStore(time.Hour, nil)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			for j := 0; j < 200; j++ {
				s.ApplySpans(healthSpan(SpanBluetoothHealth, map[string]float64{
					AttrEventGapMs:        float64(j),
					AttrActiveControllers: 2,
				}, "rust", []sampleEvent{
					{serial: "A", hz: float64(j), droppedPct: 0.1, adapter: "python"},
					{serial: "B", hz: float64(j % 30), droppedPct: 0.2, adapter: "rust"},
				}, j%2 == 0))
				_ = s.Snapshot()
				s.EvictStale()
			}
		}(i)
	}
	wg.Wait()
}
