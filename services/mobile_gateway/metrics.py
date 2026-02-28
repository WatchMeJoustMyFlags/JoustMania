"""
OTEL Push Metrics for Mobile Gateway (Issue #594).

Tracks WebSocket connections and latency for phone-based controllers.
"""

from lib.otel_metrics import Counter, Gauge, Histogram

# Connection metrics
mobile_connections_active = Gauge(
    "mobile_connections_active",
    "Number of currently active phone WebSocket connections",
)

mobile_connections_total = Counter(
    "mobile_connections_total",
    "Total phone WebSocket connections established",
)

mobile_ws_latency_seconds = Histogram(
    "mobile_ws_latency_seconds",
    "WebSocket message processing latency",
    buckets=[0.001, 0.005, 0.010, 0.016, 0.025, 0.050, 0.100],
)

# System metrics
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")

process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")

process_threads = Gauge("process_threads", "Number of active threads")
