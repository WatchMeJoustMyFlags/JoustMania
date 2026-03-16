"""
OTEL Push Metrics for Python HID Service.

Tracks device operations, polling performance, and process health.
"""

from lib.otel_metrics import Counter, Gauge, Histogram

# Device metrics
python_hid_devices_open = Gauge("python_hid_devices_open", "Number of currently open HID devices")

python_hid_discover_total = Counter("python_hid_discover_total", "Total number of discover operations")

python_hid_open_total = Counter("python_hid_open_total", "Total number of open operations", ["success"])

# Polling metrics
python_hid_poll_duration_seconds = Histogram(
    "python_hid_poll_duration_seconds",
    "Duration of a single poll cycle across all devices",
    buckets=[0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01],
)

python_hid_sensor_data_total = Counter(
    "python_hid_sensor_data_total",
    "Total sensor data readings produced",
)

# Stream metrics
python_hid_active_streams = Gauge("python_hid_active_streams", "Number of active StreamIO connections")

# Process metrics (standard OTEL process metrics)
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")
process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")
process_threads = Gauge("process_threads", "Number of active threads")
