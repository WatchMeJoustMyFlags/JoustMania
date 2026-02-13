# Controller Backend Architecture

## Overview

The controller manager uses a unified backend system that supports multiple platforms and testing modes through a single interface.

- Single `Dockerfile` for all modes
- `controller_backend` flagd flag selects implementation at runtime
- Clean abstraction via `ControllerBackend` interface
- Easy development with Mock backend

## Architecture

```
┌──────────────────────────────────────────┐
│     ControllerManagerServicer (gRPC)     │
│                                          │
│  - Stream controller states              │
│  - Handle LED/rumble commands            │
│  - Manage controller lifecycle           │
└────────────────┬─────────────────────────┘
                 │
                 ▼
┌──────────────────────────────────────────┐
│       ControllerBackend (Interface)       │
│                                          │
│  - initialize()                          │
│  - get_controller_state(serial)          │
│  - set_led_color(serial, rgb)            │
│  - set_rumble(serial, intensity)         │
│  - scan_controllers()                    │
│  - connect_controller(address)           │
└────────────────┬─────────────────────────┘
                 │
       ┌─────────┴─────────┬─────────────┐
       ▼                   ▼             ▼
┌─────────────┐    ┌──────────────┐ ┌──────────┐
│  Bluetooth  │    │   HidAPI     │ │   Mock   │
│   Backend   │    │   Backend    │ │  Backend │
│             │    │              │ │          │
│ Linux/BlueZ │    │  libhidapi   │ │ Testing  │
│ + psmove    │    │  (Linux)     │ │ (No HW)  │
└─────────────┘    └──────────────┘ └──────────┘
```

## Backends

### 1. BluetoothBackend (Production - Raspberry Pi)

**File**: `services/controller_manager/bluetooth_backend.py`

**Platform**: Linux (Raspberry Pi)

**Dependencies**:
- `psmove` - PS Move controller I/O
- `dbus-python` - BlueZ D-Bus communication
- `controller_state` - State tracking
- `pair` - Controller pairing

**Usage**:
```json
// services/flagd/performance.json (default)
"controller_backend": {
  "defaultVariant": "bluetooth"
}
```

**Features**:
- Full Bluetooth pairing support
- RSSI (signal strength) monitoring
- Battery level tracking
- Motion sensors (accel/gyro)
- LED + rumble control
- Controller hot-plug

**Hot-Plug Support**:

Controllers can connect/disconnect dynamically after container startup. The backend polls `psmove.count_connected()` and rescans when count changes:

```python
def get_connected_controllers(self) -> list[str]:
    count = psmove.count_connected()
    if count != self._last_controller_count:
        # Rescan with retry logic for newly connected controllers
        # New controllers may not be immediately ready - retry 3x with 0.5s delay
```

Docker requirements for hot-plug:
```yaml
controller-manager:
  privileged: true
  pid: "host"            # Required: host PID namespace for device visibility
  volumes:
    - /dev:/dev:rslave   # Required: rslave propagation for new devices
```

### 2. MockBackend (Testing/CI)

**File**: `services/controller_manager/mock_backend.py`

**Platform**: Any (pure Python)

**Dependencies**: None

**Usage**:
```json
// services/flagd/performance.ci.json (CI default)
"controller_backend": {
  "defaultVariant": "mock"
}
```

Or use `make up-mock` which applies the CI flagd config.

**Features**:
- Simulates 1-N controllers
- Random button presses
- Realistic motion sensor noise
- Battery drain simulation
- LED/rumble state tracking (no output)
- **No hardware required**

**Use Cases**:
- CI/CD pipelines
- Integration tests
- Development without controllers
- Automated testing

## Backend Selection

### Priority

1. **OpenFeature flag** (`controller_backend` in performance domain) - runtime-switchable via flagd
2. **Default** - Linux bluetooth backend

### Fallback

If the flagd flag is empty or flagd is unavailable, the system defaults to `BluetoothBackend`.

### Manual Override

Set the backend via flagd flag in `services/flagd/performance.json`:

```json
"controller_backend": {
  "defaultVariant": "mock"
}
```

## Docker Compose Integration

### Production (docker-compose.yml)

```yaml
controller-manager:
  dockerfile: services/controller_manager/Dockerfile
  privileged: true  # Bluetooth access
  devices:
    - /dev/bus/usb  # USB pairing
  # controller_backend defaults to "bluetooth" in flagd performance.json
```

### Testing (docker-compose.ci.yml)

```yaml
# Uses performance.ci.json with controller_backend=mock
flagd:
  volumes:
    - ./services/flagd/performance.ci.json:/etc/flagd/performance.json
```

### Mock Mode

```bash
make up-mock  # Uses CI flagd config (controller_backend=mock)
```

## Benefits

### 1. **Single Dockerfile**
- No more `Dockerfile.mock`
- Backend selected at runtime via flagd
- Reduces maintenance burden

### 2. **Clean Testing**
- Mock backend has zero hardware dependencies
- Runs in CI without special setup
- Consistent behavior across environments

### 3. **No Code Changes for Mock**
- Set `controller_backend=mock` in flagd -> instant mock mode
- No conditional code in service logic
- Clean separation of concerns

## Configuration (flagd)

| Flag | Domain | Values | Default | Description |
|------|--------|--------|---------|-------------|
| `controller_backend` | performance | `bluetooth`, `mock`, `hidapi` | `bluetooth` | Select backend |
| `mock_controller_count` | performance | 2, 4, 6, 8 | 4 | Mock controllers count |

## See Also

- [ControllerBackend Interface](../../services/controller_manager/backend.py)
