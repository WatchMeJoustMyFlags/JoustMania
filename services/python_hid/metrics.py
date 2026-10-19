"""
OTEL Push Metrics for Python HID Service.

Standard process metrics only.
"""

from lib.otel_metrics import Counter, Gauge

# Process metrics (standard OTEL process metrics)
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")
process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")
process_threads = Gauge("process_threads", "Number of OS threads in the process")
