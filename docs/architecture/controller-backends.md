# Controller Backend Architecture

## Overview

The controller manager uses a two-layer backend system:

1. **ControllerBackend** — high-level async interface used by the servicer
2. **ControllerIOAdapter** — thin sync I/O interface for raw hardware communication

`MultiplexerBackend` orchestrates one or more adapters with centralized state tracking.

## Architecture

```
┌──────────────────────────────────────────────────┐
│          ControllerManagerServicer (gRPC)          │
└──────────────────┬───────────────────────────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────┐
│     MultiplexerBackend (implements ControllerBackend)│
│                                                    │
│  Centralized state:                                │
│  - LED colors, rumble, effects per serial          │
│  - Adapter assignment (serial → adapter)           │
│  - LED keep-alive (4s refresh)                     │
│                                                    │
│  CentralizedBTDiscovery (optional)                 │
│  - Multi-adapter Bluetooth scanning                │
│  - Adapter affinity tracking                       │
└──────────────┬───────────────────────────────────┘
               │
     ┌────────┴────────┬────────────┐
     ▼                ▼            ▼
  ┌────────┐   ┌──────────┐  ┌────────┐
  │ Python │   │  Rust   │  │  Mock  │
  │Adapter │   │ Adapter  │  │Adapter │
  │        │   │          │  │        │
  │ gRPC → │   │ gRPC →  │  │Testing │
  │python- │   │ rust-hid │  │(No HW) │
  │  hid   │   │          │  │        │
  └────────┘   └──────────┘  └────────┘
```

## Adapters (ControllerIOAdapter)

Adapters handle device handles and raw I/O only. All methods are sync (blocking) — called via `asyncio.to_thread()`. State tracking (LED colors, rumble, effects) lives in `MultiplexerBackend`.

**Interface** (`multiplexer/adapter.py`):
```python
class ControllerIOAdapter(ABC):
    adapter_type: str              # "python", "rust", "mock"
    def discover(force=False) -> list[str]
    def open(serial) -> bool
    def poll(serial) -> dict | None
    def set_output(serial, r, g, b, rumble) -> bool
    def close(serial) -> None
    def close_all() -> None
```

`set_output()` combines LED + rumble in one call — this matches HID output report reality and prevents rumble from being reset when LEDs refresh.

### PythonHidAdapter

**File**: `multiplexer/python_hid_adapter.py`

Delegates all HID I/O to the `python-hid` gRPC service (port 50059). Uses sync `grpc.insecure_channel` (called via `asyncio.to_thread()` like other adapters). Architecturally mirrors `RustAdapter` — both are gRPC clients to standalone HID services implementing `psmove_hid.proto`.

- Environment variables: `PYTHON_HID_HOST` (default: `localhost`), `PYTHON_HID_PORT` (default: `50059`)

### MockAdapter

**File**: `multiplexer/mock_adapter.py`

Simulated I/O for testing without hardware. Extra methods for `MockControllerService`: `add_controller()`, `remove_controller()`, `add_observer()`, `get_led_color()`.

### RustAdapter

**File**: `multiplexer/rust_adapter.py`

Delegates all HID I/O to the `rust-hid` gRPC service (port 50058). Uses sync `grpc.insecure_channel` (called via `asyncio.to_thread()` like other adapters). A background thread manages a bidirectional `StreamIO` stream for continuous sensor data and output commands.

- `poll()` reads from a cached sensor data dict (last-write-wins per serial)
- `set_output()` queues an `IOCommand` on a bounded queue (capacity 256)
- Environment variables: `RUST_HID_HOST` (default: `localhost`), `RUST_HID_PORT` (default: `50058`)

## Multi-Adapter Support

The `backend` flag (in the `controller` domain) accepts comma-separated values to run multiple adapters simultaneously:

```json
"backend": { "defaultVariant": "python_rust" }
```

This creates a `MultiplexerBackend` with a `PythonHidAdapter` and a `RustServiceAdapter`. A `MockAdapter` (zero controllers) is always added, so the flag has no `mock_*` variants (removed in #1322). Variants:
- `mock` — mock only
- `rust` — rust-hid (+ mock) **(default, #1371)**
- `python` — python-hid (+ mock) *(opt-in)*
- `python_rust` (`python,rust`) — python-hid + rust-hid side by side (+ mock) *(opt-in)*

### python-hid is opt-in (#1371)

rust-hid is the default Bluetooth backend everywhere (UC-002 BR-003). The Python backend is slower and gets no new work; it stays selectable only until it is removed after the talk, together with the infra rollout loop (`UnstableAdapter`, the rollout router, `rollout.json`, agent remediation).

- The `python-hid` compose service carries the `python-hid` profile, so it starts only when you opt in. The controller manager no longer waits for it (`depends_on` with `required: false`).
- To read real controllers through python-hid you need all three (#1402 S1):
  1. the `python-hid` compose profile,
  2. `backend=python_rust` (or another variant naming `python`), so the python adapter is loaded, **and**
  3. `bluetooth_backend=python`, so serials (and USB pairing, UC-001 BR-003) are routed to it. Its default `rust` keeps every controller on rust-hid even with the python adapter loaded.

  ```bash
  ./scripts/setup/joustmania-compose.sh --profile python-hid up -d   # or COMPOSE_PROFILES=python-hid
  # controller.json: "backend" defaultVariant -> python_rust, then scripts/flags.sh restart and restart controller-manager
  scripts/flags.sh set controller bluetooth_backend python          # stack overlay (or the baseline defaultVariant)
  ```

  On the Pi, set the profile in a systemd drop-in (`sudo systemctl edit joustmania`, `[Service]` `Environment=COMPOSE_PROFILES=python-hid`), not in `.env`: `.env` is tracked, and editing it breaks the start script's `git pull --ff-only` (#1402 S2). When the profile is off, `scripts/setup/joustmania-start.sh` removes a stale `joustmania-python-hid` container left from an older install (#1402 I1).

- `python_rust` stays as an opt-in variant: it is the only way to run both HID services side by side, which the remaining rollout loop (UC-050 to UC-052) needs until both are removed after the talk. They are no longer a default anywhere. The backend-comparison dashboard was deleted in #1427.
- `bluetooth_backend=unstable` routes through the `UnstableAdapter`, which wraps the python adapter, so it also needs a `python` backend variant.

## Backend Selection

### Priority

1. **OpenFeature flag** (`backend` in the `controller` domain), read once at controller-manager startup
2. **Default** — `rust` (rust-hid + the auto-injected mock)

### Fallback

If the flagd flag is empty or flagd is unavailable, the controller manager falls back to `rust` (`backend_factory.py`).

## Configuration (flagd)

| Flag | Domain | Values | Default | Description |
|------|--------|--------|---------|-------------|
| `backend` | controller | `mock`, `python`, `rust`, comma-separated | `rust` | Select backend(s); `python` variants are opt-in |
| `bluetooth_backend` | controller | `python`, `rust`, `unstable` | `rust` | Per-serial Bluetooth adapter routing (canary rollout target); also selects the pairing daemon's backend (below) |

## Docker Compose Integration

### Production (docker-compose.yml) and the Pi lite stack (docker-compose.lite.yml)

Both stacks start `rust-hid` (privileged, host PID namespace, `/dev:/dev:rslave` for hidraw hot-plug, gRPC 50058) and make the controller manager and the pairing daemon wait until it is healthy. `python-hid` exists only in `docker-compose.yml`, behind the `python-hid` profile.

| | full (`docker-compose.yml`) | lite (`docker-compose.lite.yml`) |
|---|---|---|
| rust-hid | always | always (#1318) |
| python-hid | `--profile python-hid` | not shipped |
| controller-manager → rust-hid | `RUST_HID_HOST=rust-hid` | `RUST_HID_HOST=rust-hid` |
| pairing-daemon → rust-hid | host network, `127.0.0.1:50058` (published on loopback only, #1319) | host network, `127.0.0.1:50058` (published on loopback only, #1402) |

### Pairing daemon backend (UC-001 BR-003, #1320)

The pairing daemon resolves `bluetooth_backend` once per USB poll and maps every variant:

| `bluetooth_backend` | Pairing backend |
|---|---|
| `rust` (default) | `RustServiceBackend` → rust-hid PairingService |
| `python` | `HidapiBackend` (in-process hidapi) |
| `unstable` | `HidapiBackend` (`unstable` wraps the python I/O path; pairing has nothing to inject) |
| anything else, or flagd down | `RustServiceBackend` (warning logged) |

### Mock Mode

```bash
make up-mock  # Uses CI flagd config (backend=mock)
```

## See Also

- [ControllerBackend Interface](../../services/controller_manager/backend.py)
- [ControllerIOAdapter ABC](../../services/controller_manager/multiplexer/adapter.py)
- [MultiplexerBackend](../../services/controller_manager/multiplexer/multiplexer_backend.py)
- [PythonHidAdapter](../../services/controller_manager/multiplexer/python_hid_adapter.py)
- [RustAdapter](../../services/controller_manager/multiplexer/rust_adapter.py)

## History

The controller backend originally used [psmoveapi](https://github.com/thp/psmoveapi) (C library with SWIG Python bindings) for PS Move communication. This required a separate `psmove-builder` Docker image to compile the C library from source. The migration to hidapi (pure Python HID via the `hidraw` kernel interface) eliminated this build dependency while maintaining identical controller behavior. The in-process `HidapiAdapter` was subsequently extracted into a standalone `python-hid` gRPC service (port 50059), mirroring the `rust-hid` architecture. The controller manager now communicates with both services via gRPC through `PythonHidAdapter` and `RustAdapter`. The HID report format documentation in `lib/psmove_hid.py` was derived from psmoveapi's protocol implementation.
