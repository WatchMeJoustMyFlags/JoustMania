"""
OTEL push metrics for Menu Service.

Tracks system resources and gRPC request performance.
"""

from lib.otel_metrics import Counter, Gauge, Histogram

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

# Button monitoring metrics (Phase 56: Event-driven spans)
button_frames_processed_total = Counter(
    "menu_button_frames_processed_total",
    "Total number of controller state frames processed by button monitor",
)

button_presses_total = Counter(
    "menu_button_presses_total",
    "Total number of button presses detected",
    ["button", "action"],  # button: 'trigger', 'move', 'select', 'start', etc. action: 'press', 'hold'
)

lobby_updates_total = Counter(
    "menu_lobby_updates_total",
    "Total number of lobby feedback updates sent",
)

# Stream metrics (Phase 59)
stream_connections_active = Gauge(
    "menu_stream_connections_active",
    "Number of active StreamMenuEvents connections",
)

stream_events_published_total = Counter(
    "menu_stream_events_published_total",
    "Total events published to stream subscribers",
    ["event_type"],
)

# Stream reconnection metrics (Issue #330)
game_event_stream_reconnects_total = Counter(
    "menu_game_event_stream_reconnects_total",
    "Total game event stream reconnection attempts",
)

# Player ready state metrics (Phase 75: Per-player insights)
player_ready = Gauge(
    "menu_player_ready",
    "Player ready status in lobby (1=ready, 0=not ready)",
    ["serial"],
)

# Stream health metrics (Issue #337)
stream_publish_drops_total = Counter(
    "menu_stream_publish_drops_total",
    "Total menu events dropped due to full subscriber queues",
    ["event_type"],
)

stream_disconnections_total = Counter(
    "menu_stream_disconnections_total",
    "Total menu event stream disconnections",
)

# Idle mode metrics
idle_mode_active = Gauge(
    "menu_idle_mode_active",
    "Whether idle mode is currently engaged (1=active, 0=inactive)",
)

idle_controllers_count = Gauge(
    "menu_idle_controllers_count",
    "Number of controllers currently in idle state",
)

sentinel_controllers_count = Gauge(
    "menu_sentinel_controllers_count",
    "Number of active sentinel controllers",
)

idle_mode_entries_total = Counter(
    "menu_idle_mode_entries_total",
    "Total times idle mode has been entered",
)

idle_mode_wake_total = Counter(
    "menu_idle_mode_wake_total",
    "Total times controllers have been woken from idle mode",
)

# Ghost controller reconciliation metrics
ghost_controllers_pruned_total = Counter(
    "menu_ghost_controllers_pruned_total",
    "Total ghost controllers pruned via reconciliation with backend",
)
