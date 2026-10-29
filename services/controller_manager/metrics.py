"""
OTEL Push Metrics for Controller Manager (Issue #104).

Tracks controller health, input latency, stream performance, and cache efficiency.
Uses OTLP push at 100ms intervals for real-time dashboard updates.
"""

from lib.otel_metrics import Counter, Gauge, Histogram

# Controller health metrics
controller_battery_level = Gauge("controller_battery_level", "Controller battery level (0-5)", ["serial"])

# Normalized battery percentage (#730, #722 §7). The 0-5 scale above is kept for
# compatibility; this exposes 0-100 so policy.battery_threshold (expressed in
# percent) is enforceable and the agent reads one consistent unit.
controller_battery_pct = Gauge("controller_battery_pct", "Controller battery level (0-100)", ["serial"])


def battery_to_pct(battery: float) -> float:
    """
    Normalize a controller battery reading to a 0-100 percentage (#730).

    Two sources feed battery values:
    - The psmoveapi path reports a 0-5 charge level -> multiply by 20.
    - The Rust HID path (proto/psmove_hid.proto) already reports 0-100 -> pass
      through unchanged.

    Heuristic: values in the 0-5 range are treated as the coarse scale; anything
    above 5 is assumed to already be a percentage. Result is clamped to [0, 100].

    Note: charging/unknown sentinel values from psmoveapi (e.g. 0xEE/0xEF, well
    above 100) clamp to 100, matching the "full if unknown" default used by
    callers.
    """
    pct = battery * 20.0 if battery <= 5 else float(battery)
    return max(0.0, min(100.0, pct))


controller_connected = Gauge(
    "controller_connected", "Controller connection status (0=disconnected, 1=connected)", ["serial"]
)

# Controller LED color metrics (Phase 75: Per-player insights)
controller_color_r = Gauge("controller_color_r", "Controller LED red component (0-255)", ["serial"])
controller_color_g = Gauge("controller_color_g", "Controller LED green component (0-255)", ["serial"])
controller_color_b = Gauge("controller_color_b", "Controller LED blue component (0-255)", ["serial"])

# Controller info metric (Phase 75: Per-player insights)
# This is an "info" style metric - always 1, with useful labels for joins
controller_info = Gauge(
    "controller_info",
    "Controller information (always 1, use labels for joins)",
    ["serial", "name"],
)


def controller_info_name(serial: str, name: str) -> str:
    """The ``name`` label controller_info carries for a controller (Issue #74).

    The name from the name manager, else "PS Move <last 4 of serial>", else the serial.
    """
    if name:
        return name
    if len(serial) >= 4:
        return f"PS Move {serial[-4:]}"
    return serial


def set_controller_info(serial: str, name: str) -> None:
    """Publish the controller_info{serial,name} series for a tracked controller."""
    controller_info.labels(serial=serial, name=controller_info_name(serial, name)).set(1)


def remove_controller_info(serial: str, name: str) -> None:
    """Drop the controller_info{serial,name} series (rename or untrack, #1295).

    Without this an old {serial,name} label set keeps being exported and a consumer
    (the agent's player.name view) cannot tell which name is current.
    """
    controller_info.remove(serial, controller_info_name(serial, name))


# Combined LED color as hex integer (Phase 75: Per-player insights)
# Value is (R << 16) | (G << 8) | B, e.g., 0xFF0000 for red
controller_color_hex = Gauge(
    "controller_color_hex",
    "Controller LED color as hex integer (R<<16 | G<<8 | B)",
    ["serial"],
)

# Backend info metric (Phase 1: MultiplexerBackend routing visibility)
# Always 1, use labels for joins to see which backend owns each controller
controller_backend_info = Gauge(
    "controller_backend_info",
    "Controller backend assignment (always 1, use labels for joins)",
    ["serial", "backend"],
)

# Bluetooth adapter metrics (Phase 3: multi-adapter support)
bluetooth_adapter_count = Gauge(
    "bluetooth_adapter_count",
    "Number of active Bluetooth adapters",
)

controller_adapter_info = Gauge(
    "controller_adapter_info",
    "Controller adapter affinity (always 1, use labels for joins)",
    ["serial", "adapter"],
)

controller_disconnect_total = Counter(
    "controller_disconnect_total", "Total number of controller disconnects", ["serial"]
)

controller_routing_decisions_total = Counter(
    "controller_routing_decisions_total",
    "Adapter routing decisions",
    ["serial", "adapter", "method"],  # method: targeted, rollout, fallback, default
)

controller_reconnect_total = Counter("controller_reconnect_total", "Total number of controller reconnects", ["serial"])

active_controllers = Gauge("active_controllers_total", "Number of currently active controllers")

# Input latency: age of the sample each gameplay frame carries, from the CM poll
# loop picking it up to the StreamGameplayData send. Stale replays keep aging, so
# a stalled backend shows up here. Excludes the Bluetooth/HID hop before the poll
# (SensorData has no hardware timestamp). Recorded on game (filtered) gameplay
# streams only, not the dashboard's unfiltered one. No dashboard panel since #1427; query it in Grafana Explore.
controller_input_lag_seconds = Histogram(
    "controller_input_lag_seconds",
    "Age of the controller sample when sent on the gameplay stream (CM poll pick-up to gRPC send)",
    ["serial"],
    buckets=[0.001, 0.005, 0.010, 0.016, 0.020, 0.030, 0.050, 0.100, 0.200, 0.500, 1.0],
)

controller_state_update_hz = Gauge("controller_state_update_hz", "Controller state update frequency", ["serial"])

# Stream metrics
active_streams = Gauge("controller_streams_active", "Number of active controller state streams")

stream_updates_total = Counter(
    "controller_stream_updates_total",
    "Total controller state updates sent",
    ["stream_type"],  # 'legacy', 'button_events', 'gameplay_data'
)

button_events_total = Counter(
    "controller_button_events_total",
    "Total button events generated",
    ["serial", "button", "action"],  # action: 'press' or 'release'
)


# System metrics
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")

process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")

process_threads = Gauge("process_threads", "Number of active threads")

# gRPC metrics
grpc_requests_total = Counter("grpc_requests_total", "Total gRPC requests received", ["method", "status"])

grpc_request_duration_seconds = Histogram(
    "grpc_request_duration_seconds",
    "gRPC request duration",
    ["method"],
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0],
)

# Adaptive controller filtering metrics (Phase 45)
streamed_controllers = Histogram(
    "controller_streamed_per_frame",
    "Number of controllers streamed per frame",
    buckets=[1, 2, 5, 10, 15, 20, 25, 30],
)

# Stream-based feedback commands (Phase 46)
stream_commands_total = Counter(
    "controller_stream_commands_total",
    "Total feedback commands received via stream",
    ["command_type"],  # 'color', 'effect', 'vibration'
)

# Discovery polling metrics (Phase 56: Event-driven spans)
discovery_checks_total = Counter(
    "controller_discovery_checks_total",
    "Total number of controller discovery checks performed",
)

discovery_check_duration_seconds = Histogram(
    "controller_discovery_check_duration_seconds",
    "Duration of controller discovery checks",
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500],
)

battery_checks_total = Counter(
    "controller_battery_checks_total",
    "Total number of battery level checks performed",
)

battery_check_duration_seconds = Histogram(
    "controller_battery_check_duration_seconds",
    "Duration of battery checks",
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100],
)

# Discovery pass counter
discovery_full_enumerate_total = Counter(
    "controller_discovery_full_enumerate_total",
    "Full discovery enumerations (with HID/psmove scanning)",
)

# Parallel polling metrics (Phase 62)
poll_batch_duration_seconds = Histogram(
    "controller_poll_batch_duration_seconds",
    "Duration to poll all controllers in parallel",
    buckets=[0.001, 0.003, 0.005, 0.010, 0.016, 0.025, 0.050, 0.100],
)

# How late each poll started relative to its 100 Hz schedule (#1447). The poll
# loop runs nothing but the poll, so lateness here is event-loop contention
# (gRPC streams, effects, health emission), not discovery or LED work.
poll_loop_lateness_seconds = Histogram(
    "controller_poll_loop_lateness_seconds",
    "How late each controller-manager poll started after its scheduled time",
    buckets=[0.0005, 0.001, 0.002, 0.005, 0.010, 0.020, 0.050, 0.100, 0.250],
)

poll_batch_size = Histogram(
    "controller_poll_batch_size",
    "Number of controllers polled per batch",
    buckets=[1, 2, 4, 8, 12, 16, 20, 24],
)

# Fixed interval polling metrics
polling_mode = Gauge(
    "controller_polling_mode",
    "Polling mode, always 1 (fixed 100 Hz loop; the 10 Hz idle mode was removed)",
)

polling_target_hz = Gauge(
    "controller_polling_target_hz",
    "Target polling rate in Hz based on current mode",
)

# LED batch update metrics (Phase 72 optimization)
led_batch_updates_total = Counter(
    "controller_led_batch_updates_total",
    "Total number of LED batch update cycles",
)

led_controllers_updated_per_batch = Histogram(
    "controller_led_controllers_updated_per_batch",
    "Number of controllers with LEDs updated per batch cycle",
    buckets=[0, 1, 2, 4, 8, 12, 16, 20, 24],
)

# High-frequency acceleration metrics (Issue #62: 100Hz for KubeCon demo)
# These are updated on every poll cycle for real-time visualization
controller_accel_magnitude = Gauge(
    "controller_accel_magnitude",
    "Controller acceleration magnitude in g (100Hz)",
    ["serial"],
)

controller_accel_x = Gauge(
    "controller_accel_x",
    "Controller X-axis acceleration in g (100Hz)",
    ["serial"],
)

controller_accel_y = Gauge(
    "controller_accel_y",
    "Controller Y-axis acceleration in g (100Hz)",
    ["serial"],
)

controller_accel_z = Gauge(
    "controller_accel_z",
    "Controller Z-axis acceleration in g (100Hz)",
    ["serial"],
)

# Stream timing metrics (jitter optimization)
stream_frame_overruns_total = Counter(
    "controller_stream_frame_overruns_total",
    "Total number of stream frames that took longer than target interval",
)

# Identify a controller (UC-004, FR-121): one increment per IdentifyController call.
controller_identify_requests_total = Counter(
    "controller_identify_requests_total",
    "IdentifyController calls by outcome (started, not_connected, in_game, reserved, invalid)",
    ["outcome"],
)

# Streaming frequency flag metrics (direct flagd integration)
stream_frequency_changes_total = Counter(
    "controller_stream_frequency_changes_total",
    "Total number of streaming frequency changes from flagd",
)

stream_current_frequency_hz = Gauge(
    "controller_stream_current_frequency_hz",
    "Current streaming frequency in Hz (from flagd override)",
)

# Per-controller health metrics (observable connection quality)
controller_poll_drops_total = Counter(
    "controller_poll_drops_total",
    "Total poll() calls returning None per controller",
    ["serial"],
)

controller_poll_errors_total = Counter(
    "controller_poll_errors_total",
    "Total poll() exceptions per controller",
    ["serial"],
)

# Bluetooth health metrics (#732 M3). Emitted once per ~1s health window
# alongside the controller.bluetooth_health span. These feed dashboards; the
# agent's OBSERVE input is the span, not these metrics.
#
# Histogram of the window-max inter-event gap (in SECONDS — repo convention is
# *_seconds for durations). The span carries the same value in ms.
controller_bluetooth_event_gap_seconds = Histogram(
    "controller_bluetooth_event_gap_seconds",
    "Worst-case inter-event gap across controllers per health window (seconds)",
    buckets=[0.010, 0.025, 0.050, 0.075, 0.100, 0.150, 0.200, 0.350, 0.500, 1.0],
)

# Per-serial dropped-event ratio (0-1) over the window, plus an unlabeled
# window aggregate (the worst-case across serials). The Gauge family supports a
# no-label observation only when the metric has no label keys, so we declare two
# separate gauges (mirroring the prom_*/labeled split used elsewhere).
controller_bluetooth_dropped_events_ratio = Gauge(
    "controller_bluetooth_dropped_events_ratio",
    "Fraction of polls returning no fresh frame per controller over the window (0-1)",
    ["serial"],
)

controller_bluetooth_dropped_events_ratio_window = Gauge(
    "controller_bluetooth_dropped_events_ratio_window",
    "Window-aggregate dropped-event ratio across all controllers (0-1)",
)

# Per-serial movement update rate: genuinely-new frames per second over the
# window. Stale replays (identical poll objects) are NOT counted as fresh, so a
# throttled/degraded backend shows a depressed rate here.
controller_bluetooth_movement_update_hz = Gauge(
    "controller_bluetooth_movement_update_hz",
    "Genuinely-new frame rate per controller over the health window (Hz)",
    ["serial"],
)

# Connection quality per controller (UC-035 BR-005, #1431): p95 and longest gap
# between fresh frames over a rolling 10 s window (the longest includes the
# still-open gap), and the class derived from them (0 good, 1 fair, 2 poor).
# Absent while the window has fewer than two fresh frames.
controller_bluetooth_report_gap_p95_seconds = Gauge(
    "controller_bluetooth_report_gap_p95_seconds",
    "p95 gap between fresh frames per controller over a rolling 10 s window (seconds)",
    ["serial"],
)
controller_bluetooth_report_gap_max_seconds = Gauge(
    "controller_bluetooth_report_gap_max_seconds",
    "Longest gap between fresh frames per controller over a rolling 10 s window, open gap included (seconds)",
    ["serial"],
)
controller_connection_quality = Gauge(
    "controller_connection_quality",
    "Connection quality class per controller (0 good, 1 fair, 2 poor; UC-035 BR-005)",
    ["serial"],
)
