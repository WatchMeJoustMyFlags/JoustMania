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
"backend": { "defaultVariant": "mock_rust" }
```

This creates a `MultiplexerBackend` with both a `MockAdapter` and a `RustServiceAdapter`, allowing real and simulated controllers in the same session. A `MockAdapter` (zero controllers) is always added, so `rust` and `mock,rust` load the same adapters. Valid combinations:
- `mock` — mock only
- `rust` — rust-hid (+ mock) **(default, #1371)**
- `mock,rust` — mock + rust-hid
- `python` — python-hid (+ mock) *(opt-in)*
- `mock,python` — mock + python-hid *(opt-in)*
- `python,rust` / `mock,python,rust` — python-hid + rust-hid side by side *(opt-in)*

### python-hid is opt-in (#1371)

rust-hid is the default Bluetooth backend everywhere (UC-002 BR-003). The Python backend is slower and gets no new work; it stays selectable only until it is removed after the talk, together with the infra rollout loop (`UnstableAdapter`, the rollout router, `rollout.json`, agent remediation).

- The `python-hid` compose service carries the `python-hid` profile, so it starts only when you opt in. The controller manager no longer waits for it (`depends_on` with `required: false`).
- To run a variant that names `python`, start the stack with the profile **and** select the variant:

  ```bash
  COMPOSE_PROFILES=python-hid docker compose up -d     # or: docker compose --profile python-hid up -d
  # then set controller.json "backend" defaultVariant to python / mock_python / python_rust, and restart controller-manager
  ```

- `python_rust` and `mock_python_rust` stay as opt-in variants: they are the only way to run both HID services side by side, which the remaining rollout loop (UC-050 to UC-052) needs until both are removed after the talk. They are no longer a default anywhere. The backend-comparison dashboard was deleted in #1427.
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
| pairing-daemon → rust-hid | host network, `localhost:50058` (published port) | bridge network, `rust-hid:50058` |

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
