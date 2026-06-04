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

### ChaosAdapter (Fault Injection)

**File**: `multiplexer/chaos_adapter.py`

Decorator that wraps any adapter to inject configurable faults for resilience testing. Uses OpenFeature fractional targeting with controller serial as `targetingKey` — flagd's consistent hashing makes targeting sticky (same serial always gets the same fault).

```
┌──────────────────┐     ┌───────────────┐
│  ChaosAdapter    │────▶│  Inner Adapter │
│  (decorator)     │     │  (any type)    │
│                  │     └───────────────┘
│  fault_map:      │
│    S1 → poll_drop│
│    S2 → none     │
│    S3 → disconnect│
└──────────────────┘
```

**Fault types:**

| Fault | `poll()` | `set_output()` | `discover()` |
|-------|----------|-----------------|---------------|
| `none` | passthrough | passthrough | passthrough |
| `poll_drop` | 30% chance → `None` | passthrough | passthrough |
| `accel_spike` | 5% chance → high accel | passthrough | passthrough |
| `led_failure` | passthrough | 50% chance → `False` | passthrough |
| `disconnect` | always `None` | always `False` | excludes serial |

**Activation:** ChaosAdapter is automatically wrapped around non-mock adapters when the `chaos_fault_type` flag exists in flagd. By default (variant `"none"`), no faults are injected — it's a pure passthrough. To activate faults, add fractional targeting to the flag:

```json
"chaos_fault_type": {
  "state": "ENABLED",
  "variants": {
    "poll_drop": "poll_drop",
    "accel_spike": "accel_spike",
    "led_failure": "led_failure",
    "disconnect": "disconnect",
    "none": "none"
  },
  "defaultVariant": "none",
  "targeting": {
    "fractional": [["poll_drop", 10], ["disconnect", 5], ["none", 85]]
  }
}
```

This would make ~10% of controllers experience poll drops, ~5% appear disconnected, and 85% behave normally. Because flagd uses consistent hashing on the targeting key (controller serial), each controller is stickily assigned to a fault bucket.

**Reactive updates:** ChaosAdapter registers a `PROVIDER_CONFIGURATION_CHANGED` listener. When you edit the targeting in flagd, all known controllers are re-evaluated within ~100ms — no restart needed.

**Metrics:** `controller_chaos_faults_injected_total{fault="..."}` counts every injected fault.

## Multi-Adapter Support

The `backend` flag (in the `controller` domain) accepts comma-separated values to run multiple adapters simultaneously:

```json
"backend": { "defaultVariant": "mock,bluetooth" }
```

This creates a `MultiplexerBackend` with both a `MockAdapter` and a `PythonHidAdapter`, allowing real and simulated controllers in the same session. Valid combinations:
- `mock` — mock only
- `python` — python-hid only
- `rust` — rust-hid only
- `python,rust` — python-hid + rust-hid (default)
- `mock,python` — mock + python-hid
- `mock,rust` — mock + rust-hid

## Backend Selection

### Priority

1. **OpenFeature flag** (`backend` in the `controller` domain) — runtime-switchable via flagd
2. **Default** — `python,rust` (both adapters loaded)

### Fallback

If the flagd flag is empty or flagd is unavailable, the system defaults to the `python,rust` backend configuration.

## Configuration (flagd)

| Flag | Domain | Values | Default | Description |
|------|--------|--------|---------|-------------|
| `backend` | controller | `mock`, `python`, `rust`, comma-separated | `python,rust` | Select backend(s) |
| `bluetooth_backend` | controller | `python`, `rust`, `unstable` | `python` | Per-serial Bluetooth adapter routing (canary rollout target) |
| `chaos_fault_type` | controller | `none`, `poll_drop`, `accel_spike`, `led_failure`, `disconnect` | `none` | Fault injection (use fractional targeting) |

## Docker Compose Integration

### Production (docker-compose.yml)

```yaml
controller-manager:
  dockerfile: services/controller_manager/Dockerfile
  privileged: true  # Bluetooth access
  devices:
    - /dev/bus/usb  # USB pairing
```

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
