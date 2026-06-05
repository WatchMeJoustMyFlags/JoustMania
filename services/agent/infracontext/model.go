// Package infracontext holds the accumulated, denormalized view of the Bluetooth
// transport health the agent builds up from the periodic
// controller.bluetooth_health span emitted by controller-manager (PR #785).
//
// It is the infrastructure-domain parallel to gamecontext: an OBSERVE-only store
// today (M3 PR C, #733). Fitness/decisions/remediation land in later stacked PRs;
// this package only recognizes the health span, extracts its signals, and exposes
// a thread-safe snapshot for the (initially stub) infrastructure evaluation loop.
//
// Pointer fields distinguish "never observed" (nil) from a genuine zero value
// (e.g. a 0.0 drop ratio): the producer may omit any window attribute, and the
// extractor sets a pointer only for attributes actually present.
package infracontext

import "time"

// ControllerHealth is the per-controller (per-serial) Bluetooth view, derived
// from the bluetooth_controller_sample span events on the health span.
type ControllerHealth struct {
	// Serial is the controller serial (controller.serial event attribute).
	Serial string

	// MovementUpdateHz is the per-serial fresh movement-frame rate.
	// Source: bluetooth.movement_update_hz (per-sample event attribute).
	MovementUpdateHz *float64

	// DroppedEventsPct is the per-serial drop ratio (0-1).
	// Source: bluetooth.dropped_events_pct (per-sample event attribute).
	DroppedEventsPct *float64

	// Adapter is the winning adapter_type for this controller
	// (e.g. "python"/"rust"/"unstable").
	// Source: controller.adapter (per-sample event attribute).
	Adapter string

	// LastUpdate is the last time this controller appeared in a health span.
	LastUpdate time.Time
}

// WindowSignals is the aggregate, window-level view carried on the
// controller.bluetooth_health span attributes (~1Hz window).
type WindowSignals struct {
	// EventGapMs is the window max inter-fresh-frame gap in milliseconds.
	// Source: bluetooth.event_gap_ms.
	EventGapMs *float64

	// DroppedEventsPct is the window drop ratio (0-1).
	// Source: bluetooth.dropped_events_pct.
	DroppedEventsPct *float64

	// MovementUpdateHz is the window min per-serial fresh rate.
	// Source: bluetooth.movement_update_hz.
	MovementUpdateHz *float64

	// ActiveControllers is the number of active controllers this window.
	// Source: bluetooth.active_controllers.
	ActiveControllers *int

	// TargetBackend is the rollout target adapter_type, "" when rollout is off.
	// Source: bluetooth.target_backend.
	TargetBackend string

	// RolloutCount is the current_controller_count under rollout, 0 when off.
	// Source: bluetooth.rollout_count.
	RolloutCount int
}

// InfraContext is the full accumulated infrastructure view: the latest window
// signals plus the live set of per-controller health records.
type InfraContext struct {
	Window      WindowSignals
	Controllers map[string]*ControllerHealth
	UpdatedAt   time.Time
}
