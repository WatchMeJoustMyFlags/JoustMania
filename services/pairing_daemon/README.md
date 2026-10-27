# PS Move Pairing Daemon

Automatic PS Move controller pairing and Bluetooth monitoring for JoustMania on Raspberry Pi.

## Overview

This daemon runs as the `pairing-daemon` container of the compose stack (`docker-compose.yml`,
`network_mode: host`, with `/var/lib/bluetooth`, `/dev` and the system D-Bus socket mounted). There is no
host systemd service, `install.sh` or `psmove-pairing.service`. It provides:

- **USB Pairing**: Automatically pairs PS Move controllers when connected via USB
- **Bluetooth Monitoring**: Tracks connected controllers, signal strength (RSSI), and connection status
- **Observability**: OpenTelemetry metrics and tracing (OTLP push)

## Usage

### Pairing a Controller

1. Plug in PS Move via USB
2. Check the logs (`docker compose logs -f pairing-daemon`); `PAIRING FAILED` means the next poll retries.
   The controller's LED shows the pairing state while it is on USB: slow white pulse, three green flashes when
   paired, steady red when pairing failed (UC-001 BR-010).
3. Unplug USB cable
4. Press PS button to connect via Bluetooth

### Service Commands

```bash
docker compose ps pairing-daemon
docker compose logs -f pairing-daemon
docker compose restart pairing-daemon
```

## Configuration

Environment variables (can be set in the `pairing-daemon` service of `docker-compose.yml`; the stock compose file sets only `METRICS_PORT` and the OTEL variables, the rest use the defaults):

| Variable | Default | Description |
|----------|---------|-------------|
| `METRICS_PORT` | `8002` | Port of the health and unpair endpoints (no `/metrics`) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTLP collector endpoint |

## Metrics

Metrics are pushed over OTLP to the collector; the daemon serves no `/metrics` endpoint (see
[CONTROLLER_CONNECTIVITY](../../docs/CONTROLLER_CONNECTIVITY.md)). Port 8002 serves health and unpair only.

### Pairing Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `psmove_pairing_attempts_total` | Counter | Total pairing attempts |
| `psmove_pairing_success_total` | Counter | Successful pairings |
| `psmove_pairing_failed_total` | Counter | Failed pairings |
| `psmove_pairing_usb_controllers` | Gauge | Currently connected USB controllers |
| `psmove_pairing_duration_seconds` | Histogram | Time to complete pairing |

### Bluetooth Monitoring Metrics (Host HCI Layer)

These metrics measure the raw Bluetooth HCI layer on the host, distinct from
`controller_*` metrics in controller-manager which measure the application layer.

| Metric | Labels | Description |
|--------|--------|-------------|
| `bluetooth_device_rssi_dbm` | `serial`, `hci_adapter` | Signal strength in dBm |
| `bluetooth_device_connected` | `serial`, `hci_adapter` | HCI connection status (1=connected) |
| `bluetooth_device_last_seen_timestamp` | `serial`, `hci_adapter` | Unix timestamp when last seen |
| `bluetooth_adapter_connections` | `hci_adapter` | Controllers per adapter |

### Example Grafana Queries

```promql
# RSSI over time per controller (host Bluetooth layer)
bluetooth_device_rssi_dbm

# Controllers per adapter
bluetooth_adapter_connections

# Time since last seen at HCI layer (staleness detection)
time() - bluetooth_device_last_seen_timestamp

# Compare HCI vs application layer connection status
# (useful for debugging connection issues)
bluetooth_device_connected == 1 and controller_connected == 0
```

## Files

| File | Purpose |
|------|---------|
| `server.py` | Main entry point |
| `psmove_pairing/` | Package directory |
| `psmove_pairing/config.py` | Configuration and constants |
| `psmove_pairing/metrics.py` | Metrics definitions (exported via OTLP) |
| `psmove_pairing/telemetry.py` | OpenTelemetry initialization |
| `psmove_pairing/utils.py` | Utility functions |
| `psmove_pairing/usb_pairing.py` | USB pairing logic |
| `psmove_pairing/bluetooth_monitor.py` | Bluetooth monitoring |
| `psmove_pairing/adapter_manager.py` | Bluetooth adapter load balancing |
| `psmove_pairing/daemon.py` | Main daemon class |
| `Dockerfile`, `server.py` | Container image and entry point |
| `pyproject.toml` | Python dependencies |
| `tests/` | Unit tests |

## Requirements

- Python 3.14
- psmoveapi (`psmove` CLI)
- BlueZ (`bluetoothctl`, `hciconfig`, `hcitool`)
- Runs in the compose stack; the host needs BlueZ and D-Bus

## Troubleshooting

**Daemon not detecting controller:**
```bash
# Check USB device
lsusb | grep Sony

# Check daemon logs
docker compose logs -f pairing-daemon
```

**Pairing succeeds but Bluetooth won't connect:**
```bash
# Check ClassicBondedOnly setting
grep ClassicBondedOnly /etc/bluetooth/input.conf
# Must be: ClassicBondedOnly=false

# Restart Bluetooth
sudo systemctl restart bluetooth
```

**No RSSI data:**
```bash
# Verify hcitool works
hcitool con   # List connections
hcitool rssi <MAC_ADDRESS>   # Get RSSI for connected device
```

See `docs/hardware-setup-guide.md` for detailed troubleshooting.

## Unpair a controller (UC-005)

`POST /controllers/{address}/unpair` on the daemon's HTTP port (8002) removes
the address's registrations on every adapter (BlueZ `RemoveDevice`, plus
`/var/lib/bluetooth/<adapter>/<address>` directories of adapters BlueZ does not
present) and forgets that the controller was paired this session. Only an
address with a PS Move registration is touched; identity-less copies of that
address on other adapters go with it.

| Status | Meaning |
|---|---|
| 200 | every registration found is `removed` or `already_absent` (idempotent); body `{"address", "removed", "already_absent", "failed", "adapters", "results", "on_usb"}` |
| 400 | not a Bluetooth address (e.g. a mock serial) |
| 404 | no registration found |
| 409 | a USB pairing or another unpair is running; retry |
| 503 | a removal failed (same body plus `error`), a BlueZ / D-Bus error, or any unexpected error |

A controller unpaired while plugged in over USB (`on_usb: true`) is **not**
paired again until it is unplugged and plugged back in, so the unpair is not
undone right away. (Pairing never restarts the Bluetooth service or power-cycles
an adapter, so it is safe while a game runs: UC-001 BR-005.) The daemon notices the
unplug on its next USB poll (default every 10 s), or by a new HID device path,
so leave it unplugged for about 10 s.

The dashboard calls it through envoy and the connect-proxy
(`POST /pairing/unpair` with `{"address": "..."}`):

    curl -X POST http://<pi>/pairing/unpair -d '{"address":"00:06:F7:AA:BB:CC"}'

**Trust model.** There is no authentication: whoever reaches the stack is the
operator. Keep the Pi on a trusted network (the talk uses a phone hotspot).
Exposure is narrowed by:

- `CONTROL_BIND` (comma-separated addresses, default `127.0.0.1`): the daemon's
  HTTP port binds only there. The full stack sets `127.0.0.1,172.17.0.1`
  (loopback + the docker0 gateway, which `host.docker.internal` resolves to for
  the connect-proxy; override with `PAIRING_CONTROL_BIND` if your Docker bridge
  uses another subnet), so the LAN cannot reach port 8002. The lite stack runs
  the daemon on the Compose network and binds `0.0.0.0` there (port not
  published).
  An address that cannot be bound at start (docker0 not up yet) is retried
  in the background (1 s doubling to 30 s); `GET /healthz` shows
  `control_bind.requested` and `control_bind.bound`, and the gauge
  `pairing_control_bind_unbound_addresses` counts the missing ones (#1450).
- The connect-proxy rejects browser requests from another origin (403), so a
  web page open on the operator's laptop cannot unpair controllers. It also
  answers 403 when the request's Host is not an IP, `localhost`, the Pi's host
  name / `<host name>.local` (from the host's `/etc/hostname`, mounted
  read-only) or a name in `JOUSTMANIA_HOSTNAMES` (DNS rebinding, #1450): open
  the dashboard by one of those, or add your name to `JOUSTMANIA_HOSTNAMES` in
  `.env.local`.
- A host firewall (e.g. ufw) must allow the Docker bridge to reach host port
  8002, or the connect-proxy answers 502.

After a re-pair to another adapter (UC-001 A4) the daemon also removes the
controller's registrations on the other adapters (UC-001 A5, BR-004). Both use
`remove_controller_registrations` in `psmove_pairing/registrations.py`, on top
of the registration reset's `clear_psmove_registrations`.
