package infracontext

import (
	"testing"
	"time"

	"go.opentelemetry.io/collector/pdata/ptrace"
)

// newTestStore makes a Store with a generous TTL and a fixed clock for tests.
func newTestStore() *Store {
	return NewStore(time.Hour, func() time.Time {
		return time.Unix(1000, 0)
	})
}

// sampleEvent describes a per-serial bluetooth_controller_sample event.
type sampleEvent struct {
	serial     string
	hz         float64
	droppedPct float64
	adapter    string
}

// healthSpan builds a controller.bluetooth_health span with the given window
// attributes and per-serial sample events. Window attrs are applied as Doubles
// (active/rollout as Ints) — useInt re-encodes the numeric window attrs as Ints
// to exercise the Int-vs-Double tolerance.
func healthSpan(name string, win map[string]float64, target string, samples []sampleEvent, useInt bool) ptrace.Traces {
	td := ptrace.NewTraces()
	span := td.ResourceSpans().AppendEmpty().ScopeSpans().AppendEmpty().Spans().AppendEmpty()
	span.SetName(name)
	attrs := span.Attributes()
	for k, v := range win {
		if useInt {
			attrs.PutInt(k, int64(v))
		} else {
			attrs.PutDouble(k, v)
		}
	}
	if target != "" {
		attrs.PutStr(AttrTargetBackend, target)
	}
	for _, s := range samples {
		ev := span.Events().AppendEmpty()
		ev.SetName(EventControllerSample)
		ev.Attributes().PutStr(AttrControllerSerial, s.serial)
		ev.Attributes().PutDouble(AttrMovementUpdateHz, s.hz)
		ev.Attributes().PutDouble(AttrDroppedEventsPct, s.droppedPct)
		ev.Attributes().PutStr(AttrControllerAdapter, s.adapter)
	}
	return td
}

func TestApplySpans_FullHealthSpan(t *testing.T) {
	s := newTestStore()
	td := healthSpan(SpanBluetoothHealth, map[string]float64{
		AttrEventGapMs:        42.5,
		AttrDroppedEventsPct:  0.1,
		AttrMovementUpdateHz:  58.0,
		AttrActiveControllers: 2,
		AttrRolloutCount:      2,
	}, "rust", []sampleEvent{
		{serial: "A", hz: 60, droppedPct: 0.0, adapter: "python"},
		{serial: "B", hz: 55, droppedPct: 0.2, adapter: "rust"},
	}, false)

	if !s.ApplySpans(td) {
		t.Fatal("expected health span to be recognized")
	}
	snap := s.Snapshot()

	if snap.Window.EventGapMs == nil || *snap.Window.EventGapMs != 42.5 {
		t.Fatalf("event_gap_ms = %v, want 42.5", snap.Window.EventGapMs)
	}
	if snap.Window.DroppedEventsPct == nil || *snap.Window.DroppedEventsPct != 0.1 {
		t.Fatalf("window dropped_events_pct = %v, want 0.1", snap.Window.DroppedEventsPct)
	}
	if snap.Window.MovementUpdateHz == nil || *snap.Window.MovementUpdateHz != 58.0 {
		t.Fatalf("window movement_update_hz = %v, want 58.0", snap.Window.MovementUpdateHz)
	}
	if snap.Window.ActiveControllers == nil || *snap.Window.ActiveControllers != 2 {
		t.Fatalf("active_controllers = %v, want 2", snap.Window.ActiveControllers)
	}
	if snap.Window.TargetBackend != "rust" {
		t.Fatalf("target_backend = %q, want rust", snap.Window.TargetBackend)
	}
	if snap.Window.RolloutCount != 2 {
		t.Fatalf("rollout_count = %d, want 2", snap.Window.RolloutCount)
	}

	if len(snap.Controllers) != 2 {
		t.Fatalf("controllers = %d, want 2", len(snap.Controllers))
	}
	a := snap.Controllers["A"]
	if a == nil || a.MovementUpdateHz == nil || *a.MovementUpdateHz != 60 || a.Adapter != "python" {
		t.Fatalf("controller A = %+v, want hz=60 adapter=python", a)
	}
	b := snap.Controllers["B"]
	if b == nil || b.DroppedEventsPct == nil || *b.DroppedEventsPct != 0.2 || b.Adapter != "rust" {
		t.Fatalf("controller B = %+v, want dropped=0.2 adapter=rust", b)
	}
}

func TestApplySpans_PartialAttrs(t *testing.T) {
	s := newTestStore()
	// Only event_gap_ms present; the rest must stay nil. No samples.
	td := healthSpan(SpanBluetoothHealth, map[string]float64{
		AttrEventGapMs: 12.0,
	}, "", nil, false)

	if !s.ApplySpans(td) {
		t.Fatal("expected partial health span to be recognized")
	}
	snap := s.Snapshot()
	if snap.Window.EventGapMs == nil || *snap.Window.EventGapMs != 12.0 {
		t.Fatalf("event_gap_ms = %v, want 12.0", snap.Window.EventGapMs)
	}
	if snap.Window.DroppedEventsPct != nil {
		t.Fatalf("dropped_events_pct = %v, want nil (absent)", snap.Window.DroppedEventsPct)
	}
	if snap.Window.MovementUpdateHz != nil {
		t.Fatalf("movement_update_hz = %v, want nil (absent)", snap.Window.MovementUpdateHz)
	}
	if snap.Window.ActiveControllers != nil {
		t.Fatalf("active_controllers = %v, want nil (absent)", snap.Window.ActiveControllers)
	}
	if snap.Window.TargetBackend != "" {
		t.Fatalf("target_backend = %q, want empty", snap.Window.TargetBackend)
	}
	if snap.Window.RolloutCount != 0 {
		t.Fatalf("rollout_count = %d, want 0", snap.Window.RolloutCount)
	}
	if len(snap.Controllers) != 0 {
		t.Fatalf("controllers = %d, want 0", len(snap.Controllers))
	}
}

func TestApplySpans_WrongSpanNameIgnored(t *testing.T) {
	s := newTestStore()
	td := healthSpan("player_lifecycle", map[string]float64{
		AttrEventGapMs: 99.0,
	}, "rust", []sampleEvent{{serial: "A", hz: 60, adapter: "python"}}, false)

	if s.ApplySpans(td) {
		t.Fatal("non-health span must not be recognized")
	}
	snap := s.Snapshot()
	if snap.Window.EventGapMs != nil {
		t.Fatal("non-health span must not populate window signals")
	}
	if len(snap.Controllers) != 0 {
		t.Fatal("non-health span must not create controllers")
	}
}

func TestApplySpans_IntVsDoubleTolerance(t *testing.T) {
	s := newTestStore()
	// All numeric window attrs encoded as Int pvalues.
	td := healthSpan(SpanBluetoothHealth, map[string]float64{
		AttrEventGapMs:        40,
		AttrDroppedEventsPct:  0, // 0 as Int
		AttrMovementUpdateHz:  58,
		AttrActiveControllers: 3,
		AttrRolloutCount:      3,
	}, "unstable", nil, true)

	if !s.ApplySpans(td) {
		t.Fatal("expected Int-encoded health span to be recognized")
	}
	snap := s.Snapshot()
	if snap.Window.EventGapMs == nil || *snap.Window.EventGapMs != 40 {
		t.Fatalf("event_gap_ms (Int) = %v, want 40", snap.Window.EventGapMs)
	}
	if snap.Window.DroppedEventsPct == nil || *snap.Window.DroppedEventsPct != 0 {
		t.Fatalf("dropped_events_pct (Int 0) = %v, want 0", snap.Window.DroppedEventsPct)
	}
	if snap.Window.MovementUpdateHz == nil || *snap.Window.MovementUpdateHz != 58 {
		t.Fatalf("movement_update_hz (Int) = %v, want 58", snap.Window.MovementUpdateHz)
	}
	if snap.Window.ActiveControllers == nil || *snap.Window.ActiveControllers != 3 {
		t.Fatalf("active_controllers (Int) = %v, want 3", snap.Window.ActiveControllers)
	}
	if snap.Window.RolloutCount != 3 {
		t.Fatalf("rollout_count (Int) = %d, want 3", snap.Window.RolloutCount)
	}
}

func TestApplySpans_SkipsOwnService(t *testing.T) {
	s := newTestStore()
	s.SetOwnService("agent")

	td := healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 10},
		"rust", []sampleEvent{{serial: "A", hz: 60, adapter: "python"}}, false)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("service.name", "agent")
	if s.ApplySpans(td) {
		t.Fatal("own-service health spans must be skipped (self-ingestion defense)")
	}
	if s.Snapshot().Window.EventGapMs != nil {
		t.Fatal("own-service span must not populate window signals")
	}

	// Other services still apply.
	td = healthSpan(SpanBluetoothHealth, map[string]float64{AttrEventGapMs: 10}, "", nil, false)
	td.ResourceSpans().At(0).Resource().Attributes().PutStr("service.name", "controller-manager")
	if !s.ApplySpans(td) {
		t.Fatal("non-own service health spans must still apply")
	}
}
