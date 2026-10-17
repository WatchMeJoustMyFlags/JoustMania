"""OTEL push metrics for PS Move pairing daemon."""

from lib.otel_metrics import Counter, Gauge, Histogram

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
pairing_adapter_selected_total = Counter(
    "psmove_pairing_adapter_selected_total",
    "Adapter selections for load-balanced pairing",
    ["adapter"],
)
pairing_adapter_device_count = Gauge(
    "psmove_pairing_adapter_device_count_at_selection",
    "Device count on adapter when selected for pairing",
    ["adapter"],
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
# Bluetooth monitoring metrics (host-level HCI layer)
# Note: These are distinct from controller_* metrics in controller-manager
# which measure the application layer (psmoveapi). These measure the raw
# Bluetooth HCI layer on the host.
bluetooth_device_rssi_dbm = Gauge(
    "bluetooth_device_rssi_dbm",
    "Bluetooth device signal strength in dBm (host HCI layer)",
    ["serial", "hci_adapter"],
)
bluetooth_device_connected = Gauge(
    "bluetooth_device_connected",
    "Bluetooth device connection status at HCI layer (1=connected, 0=disconnected)",
    ["serial", "hci_adapter"],
)
bluetooth_device_last_seen = Gauge(
    "bluetooth_device_last_seen_timestamp",
    "Unix timestamp when device was last seen connected at HCI layer",
    ["serial", "hci_adapter"],
)
bluetooth_adapter_connections = Gauge(
    "bluetooth_adapter_connections",
    "Number of controllers per adapter",
    ["hci_adapter"],
)

# Adapter identity, HCI errors and link roles (UC-035 step 6, FR-122, #1431). Read-only
# observation from hciconfig -a / hcitool con / sysfs; nothing changes a role.
pairing_bluetooth_adapter_info = Gauge(
    "pairing_bluetooth_adapter_info",
    "Bluetooth adapter identity (always 1): HCI manufacturer, HCI version, bus, USB vendor:product, onboard",
    ["hci_adapter", "address", "manufacturer", "manufacturer_id", "hci_version", "bus", "usb_id", "onboard"],
)
pairing_bluetooth_adapter_hci_errors_total = Counter(
    "pairing_bluetooth_adapter_hci_errors_total",
    "Increases of the kernel's cumulative HCI error counters per adapter (direction rx/tx)",
    ["hci_adapter", "direction"],
)
pairing_bluetooth_link_role_info = Gauge(
    "pairing_bluetooth_link_role_info",
    "Local adapter role on a controller link (always 1; role central/peripheral/unknown)",
    ["serial", "hci_adapter", "role"],
)

# Process-level system metrics (standard Prometheus naming convention)
process_cpu_seconds_total = Counter("process_cpu_seconds_total", "Total user and system CPU time spent in seconds")
process_resident_memory_bytes = Gauge("process_resident_memory_bytes", "Resident memory size in bytes")
process_threads = Gauge("process_threads", "Number of active threads")
