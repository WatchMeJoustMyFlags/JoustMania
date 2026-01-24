"""Prometheus metrics for PS Move pairing daemon."""

from prometheus_client import Counter, Gauge, Histogram

# Pairing metrics
pairing_attempts_total = Counter(
    "psmove_pairing_attempts_total",
    "Total pairing attempts",
)
pairing_success_total = Counter(
    "psmove_pairing_success_total",
    "Successful pairings",
)
pairing_failed_total = Counter(
    "psmove_pairing_failed_total",
    "Failed pairings",
)
pairing_polls_total = Counter(
    "psmove_pairing_polls_total",
    "Total polling cycles",
)
pairing_usb_controllers = Gauge(
    "psmove_pairing_usb_controllers",
    "Currently connected USB controllers",
)
pairing_duration_seconds = Histogram(
    "psmove_pairing_duration_seconds",
    "Time to complete pairing",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)
calibration_duration_seconds = Histogram(
    "psmove_pairing_calibration_duration_seconds",
    "Time to calibrate controller",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

# Bluetooth monitoring metrics
controller_rssi_dbm = Gauge(
    "controller_rssi_dbm",
    "Controller signal strength in dBm",
    ["serial", "hci_adapter"],
)
controller_connected = Gauge(
    "controller_connected",
    "Controller connection status (1=connected, 0=disconnected)",
    ["serial", "hci_adapter"],
)
controller_last_seen = Gauge(
    "controller_last_seen_timestamp",
    "Unix timestamp when controller was last seen connected",
    ["serial", "hci_adapter"],
)
bluetooth_adapter_connections = Gauge(
    "bluetooth_adapter_connections",
    "Number of controllers per adapter",
    ["hci_adapter"],
)
