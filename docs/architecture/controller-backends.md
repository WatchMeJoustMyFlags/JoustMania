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
     ┌─────────┼─────────┐
     ▼         ▼         ▼
┌──────────┐ ┌────────┐ ┌──────────┐
│ PsMove   │ │ Hidapi │ │  Mock    │
│ Adapter  │ │Adapter │ │ Adapter  │
│          │ │        │ │          │
│ psmoveapi│ │libhidapi│ │ Testing  │
│ + BlueZ  │ │(Linux) │ │ (No HW)  │
└──────────┘ └────────┘ └──────────┘
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
    adapter_type: str              # "psmove", "hidapi", "mock"
    def discover(force=False) -> list[str]
    def open(serial) -> bool
    def poll(serial) -> dict | None
    def set_output(serial, r, g, b, rumble) -> bool
    def close(serial) -> None
    def close_all() -> None
```

`set_output()` combines LED + rumble in one call — this matches HID output report reality and prevents rumble from being reset when LEDs refresh.

### PsMoveAdapter

**File**: `multiplexer/psmove_adapter.py`

Uses the `psmove` C library. Handles are opened during `discover()` since psmove uses an index-based API. Includes retry logic for flaky USB enumeration.

### HidapiAdapter

**File**: `multiplexer/hidapi_adapter.py`

Uses `hid` (hidapi) library. Reads HID input reports via `device.read()`, parses via `lib.psmove_hid.parse_input_report()`. Normalizes serials (uppercase, no colons).

### MockAdapter

**File**: `multiplexer/mock_adapter.py`

Simulated I/O for testing without hardware. Extra methods for `MockControllerService`: `add_controller()`, `remove_controller()`, `add_observer()`, `get_led_color()`.

## Multi-Adapter Support

The `controller_backend` flag accepts comma-separated values to run multiple adapters simultaneously:

```json
"controller_backend": { "defaultVariant": "mock,bluetooth" }
```

This creates a `MultiplexerBackend` with both a `MockAdapter` and a `PsMoveAdapter`, allowing real and simulated controllers in the same session. Valid combinations:
- `mock` — mock only
- `bluetooth` — psmove only
- `hidapi` — hidapi only
- `mock,bluetooth` — mock + psmove
- `mock,hidapi` — mock + hidapi

Invalid: `bluetooth,hidapi` (both use the same hardware).

## Backend Selection

### Priority

1. **OpenFeature flag** (`controller_backend` in performance domain) — runtime-switchable via flagd
2. **Default** — Linux bluetooth backend

### Multiplexer Toggle

When `multiplexer_backend_enabled` is `true`, the factory creates adapters wrapped in `MultiplexerBackend`. When `false`, legacy standalone backends are used.

### Fallback

If the flagd flag is empty or flagd is unavailable, the system defaults to `BluetoothBackend` (legacy path).

## Configuration (flagd)

| Flag | Domain | Values | Default | Description |
|------|--------|--------|---------|-------------|
| `controller_backend` | performance | `bluetooth`, `mock`, `hidapi`, comma-separated | `bluetooth` | Select backend(s) |
| `multiplexer_backend_enabled` | performance | `true`, `false` | `false` | Use adapter-based multiplexer |
| `mock_controller_count` | performance | 2, 4, 6, 8 | 4 | Mock controllers count |

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
