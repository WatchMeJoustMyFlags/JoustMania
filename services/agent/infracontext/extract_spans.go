package infracontext

import (
	"go.opentelemetry.io/collector/pdata/pcommon"
	"go.opentelemetry.io/collector/pdata/ptrace"
)

// Span name, event name, and attribute keys of the controller.bluetooth_health
// span contract (controller-manager, PR #785). Consumed VERBATIM — do not rename
// without coordinating with the producer.
const (
	// SpanBluetoothHealth is the ~1Hz root span carrying window-level Bluetooth
	// transport health. Absent when no controllers are connected.
	SpanBluetoothHealth = "controller.bluetooth_health"

	// EventControllerSample is the per-active-serial span event carrying
	// per-controller Bluetooth signals.
	EventControllerSample = "bluetooth_controller_sample"

	// Window-level span attributes.
	AttrEventGapMs        = "bluetooth.event_gap_ms"
	AttrDroppedEventsPct  = "bluetooth.dropped_events_pct"
	AttrMovementUpdateHz  = "bluetooth.movement_update_hz"
	AttrActiveControllers = "bluetooth.active_controllers"
	AttrTargetBackend     = "bluetooth.target_backend"
	AttrRolloutCount      = "bluetooth.rollout_count"

	// Per-sample event attributes. AttrDroppedEventsPct and AttrMovementUpdateHz
	// are reused at the per-serial scope.
	AttrControllerSerial  = "controller.serial"
	AttrControllerAdapter = "controller.adapter"

	// resourceAttrServiceName mirrors semconv service.name, used for the
	// own-telemetry skip.
	resourceAttrServiceName = "service.name"
)

// ApplySpans applies trace data to the store, recognizing
// controller.bluetooth_health spans and extracting their window + per-serial
// signals. Spans of any other name are ignored (the game path consumes them
// separately). It returns true if at least one health span was applied.
func (s *Store) ApplySpans(td ptrace.Traces) bool {
	updated := false
	rss := td.ResourceSpans()
	for i := 0; i < rss.Len(); i++ {
		if s.isOwnResource(rss.At(i).Resource()) {
			continue
		}
		sss := rss.At(i).ScopeSpans()
		for j := 0; j < sss.Len(); j++ {
			spans := sss.At(j).Spans()
			for k := 0; k < spans.Len(); k++ {
				span := spans.At(k)
				if span.Name() != SpanBluetoothHealth {
					continue
				}
				if s.applyHealthSpan(span) {
					updated = true
				}
			}
		}
	}
	return updated
}

// applyHealthSpan extracts one controller.bluetooth_health span: the window
// signals from the span attributes and the per-serial samples from its
// bluetooth_controller_sample events.
func (s *Store) applyHealthSpan(span ptrace.Span) bool {
	w := windowFromAttrs(span.Attributes())

	s.mu.Lock()
	s.setWindow(w)
	events := span.Events()
	for e := 0; e < events.Len(); e++ {
		ev := events.At(e)
		if ev.Name() != EventControllerSample {
			continue
		}
		if sample, ok := controllerFromAttrs(ev.Attributes()); ok {
			s.setController(sample)
		}
	}
	s.mu.Unlock()
	return true
}

// windowFromAttrs reads the six window-level attributes, setting a pointer only
// for attributes that are present. Numeric attributes tolerate both Int and
// Double pvalue encodings.
func windowFromAttrs(attrs pcommon.Map) WindowSignals {
	var w WindowSignals
	if v, ok := floatAttr(attrs, AttrEventGapMs); ok {
		w.EventGapMs = ptr(v)
	}
	if v, ok := floatAttr(attrs, AttrDroppedEventsPct); ok {
		w.DroppedEventsPct = ptr(v)
	}
	if v, ok := floatAttr(attrs, AttrMovementUpdateHz); ok {
		w.MovementUpdateHz = ptr(v)
	}
	if v, ok := intAttr(attrs, AttrActiveControllers); ok {
		w.ActiveControllers = ptr(v)
	}
	if v, ok := attrs.Get(AttrTargetBackend); ok {
		w.TargetBackend = v.AsString()
	}
	if v, ok := intAttr(attrs, AttrRolloutCount); ok {
		w.RolloutCount = v
	}
	return w
}

// controllerFromAttrs reads one per-serial sample. It returns ok=false when the
// sample carries no serial (nothing to key on).
func controllerFromAttrs(attrs pcommon.Map) (ControllerHealth, bool) {
	sv, ok := attrs.Get(AttrControllerSerial)
	if !ok {
		return ControllerHealth{}, false
	}
	serial := sv.AsString()
	if serial == "" {
		return ControllerHealth{}, false
	}
	c := ControllerHealth{Serial: serial}
	if v, ok := floatAttr(attrs, AttrMovementUpdateHz); ok {
		c.MovementUpdateHz = ptr(v)
	}
	if v, ok := floatAttr(attrs, AttrDroppedEventsPct); ok {
		c.DroppedEventsPct = ptr(v)
	}
	if v, ok := attrs.Get(AttrControllerAdapter); ok {
		c.Adapter = v.AsString()
	}
	return c, true
}

// floatAttr reads a numeric attribute as float64, tolerating both Int and Double
// pvalue encodings (mirrors gamecontext.numberValue). Returns ok=false when the
// attribute is absent or not numeric.
func floatAttr(attrs pcommon.Map, key string) (float64, bool) {
	v, ok := attrs.Get(key)
	if !ok {
		return 0, false
	}
	switch v.Type() {
	case pcommon.ValueTypeInt:
		return float64(v.Int()), true
	case pcommon.ValueTypeDouble:
		return v.Double(), true
	default:
		return 0, false
	}
}

// intAttr reads a numeric attribute as int, tolerating both Int and Double
// pvalue encodings. Returns ok=false when the attribute is absent or not numeric.
func intAttr(attrs pcommon.Map, key string) (int, bool) {
	v, ok := floatAttr(attrs, key)
	if !ok {
		return 0, false
	}
	return int(v), true
}
