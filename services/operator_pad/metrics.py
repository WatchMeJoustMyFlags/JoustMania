"""OTEL push metrics of the operator pad (telemetry.md §6, UC-015)."""

from lib.otel_metrics import Counter, Gauge

# System metrics
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")

process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")

process_threads = Gauge("process_threads", "Number of active threads")

# Pad metrics
gestures_total = Counter(
    "operator_pad_gestures_total",
    "Pad gestures that reached the action layer, by action and result (UC-015)",
    ["action", "result"],
)

previews_total = Counter(
    "operator_pad_previews_total",
    "Live-preview decisions while a layer key is held and the knob turns, by result (UC-015 BR-017)",
    ["result"],
)

unknown_keys_total = Counter(
    "operator_pad_unknown_keys_total",
    "Key codes outside operator_pad.keymap (UC-015 A4)",
)

device_attached = Gauge(
    "operator_pad_device_attached",
    "1 while at least one matching pad input node is grabbed, else 0",
)

grab_failures_total = Counter(
    "operator_pad_grab_failures_total",
    "Pad input nodes that could not be grabbed (ebusy after the retries, or another error)",
    ["reason"],
)
