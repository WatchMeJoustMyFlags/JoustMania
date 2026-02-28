# Controller Backend Architecture

## Overview

The controller manager uses a two-layer backend system:

1. **ControllerBackend** — high-level async interface used by the servicer
2. **ControllerIOAdapter** — thin sync I/O interface for raw hardware communication

When the `multiplexer_backend_enabled` flag is on (default for new deployments), `MultiplexerBackend` orchestrates one or more adapters with centralized state tracking. When off, legacy standalone backends are used directly.

## Architecture

### Multiplexer Path (recommended)

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
          ┌────┴────┐
          ▼         ▼
       ┌────────┐ ┌──────────┐
       │ Hidapi │ │  Mock    │
       │Adapter │ │ Adapter  │
       │        │ │          │
       │hidapi/ │ │ Testing  │
       │hidraw  │ │ (No HW)  │
       └────────┘ └──────────┘
```

### Legacy Path (multiplexer disabled)

```
ControllerManagerServicer → ControllerBackend (direct)
  → BluetoothBackend | HidapiBackend | MockBackend
```

## Adapters (ControllerIOAdapter)

Adapters handle device handles and raw I/O only. All methods are sync (blocking) — called via `asyncio.to_thread()`. State tracking (LED colors, rumble, effects) lives in `MultiplexerBackend`.

**Interface** (`multiplexer/adapter.py`):
```python
class ControllerIOAdapter(ABC):
    adapter_type: str              # "hidapi", "mock"
    def discover(force=False) -> list[str]
    def open(serial) -> bool
    def poll(serial) -> dict | None
    def set_output(serial, r, g, b, rumble) -> bool
    def close(serial) -> None
    def close_all() -> None
```

`set_output()` combines LED + rumble in one call — this matches HID output report reality and prevents rumble from being reset when LEDs refresh.

### HidapiAdapter

**File**: `multiplexer/hidapi_adapter.py`

Uses `hid` (hidapi) library. Reads HID input reports via `device.read()`, parses via `lib.psmove_hid.parse_input_report()`. Normalizes serials (uppercase, no colons).

### MockAdapter

**File**: `multiplexer/mock_adapter.py`

Simulated I/O for testing without hardware. Extra methods for `MockControllerService`: `add_controller()`, `remove_controller()`, `add_observer()`, `get_led_color()`.

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

The `controller_backend` flag accepts comma-separated values to run multiple adapters simultaneously:

```json
"controller_backend": { "defaultVariant": "mock,bluetooth" }
```

This creates a `MultiplexerBackend` with both a `MockAdapter` and a `HidapiAdapter`, allowing real and simulated controllers in the same session. Valid combinations:
- `mock` — mock only
- `hidapi` — hidapi only
- `mock,hidapi` — mock + hidapi

## Backend Selection

### Priority

1. **OpenFeature flag** (`controller_backend` in performance domain) — runtime-switchable via flagd
2. **Default** — hidapi backend

### Multiplexer Toggle

When `multiplexer_backend_enabled` is `true`, the factory creates adapters wrapped in `MultiplexerBackend`. When `false`, legacy standalone backends are used.

### Fallback

If the flagd flag is empty or flagd is unavailable, the system defaults to the hidapi backend.

## Configuration (flagd)

| Flag | Domain | Values | Default | Description |
|------|--------|--------|---------|-------------|
| `controller_backend` | performance | `mock`, `hidapi`, comma-separated | `hidapi` | Select backend(s) |
| `multiplexer_backend_enabled` | performance | `true`, `false` | `false` | Use adapter-based multiplexer |
| `mock_controller_count` | performance | 2, 4, 6, 8 | 4 | Mock controllers count |
| `chaos_fault_type` | performance | `none`, `poll_drop`, `accel_spike`, `led_failure`, `disconnect` | `none` | Fault injection (use fractional targeting) |

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
make up-mock  # Uses CI flagd config (controller_backend=mock)
```

## See Also

- [ControllerBackend Interface](../../services/controller_manager/backend.py)
- [ControllerIOAdapter ABC](../../services/controller_manager/multiplexer/adapter.py)
- [MultiplexerBackend](../../services/controller_manager/multiplexer/multiplexer_backend.py)

## History

The controller backend originally used [psmoveapi](https://github.com/thp/psmoveapi) (C library with SWIG Python bindings) for PS Move communication. This required a separate `psmove-builder` Docker image to compile the C library from source. The migration to hidapi (pure Python HID via the `hidraw` kernel interface) eliminated this build dependency while maintaining identical controller behavior. The HID report format documentation in `lib/psmove_hid.py` was derived from psmoveapi's protocol implementation.
