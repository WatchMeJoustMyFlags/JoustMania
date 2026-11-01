# Controller Manager Service

**Part of JoustMania Microservices Architecture**

## Overview

The Controller Manager service is the central component for managing PS Move controllers in JoustMania. It provides a gRPC API for controller discovery, state streaming, LED control, vibration feedback, and visual effects.

## Key Features

- **Controller Discovery**: Automatic detection and pairing of PS Move controllers
- **Real-time State Streaming**: 60 Hz controller state updates via gRPC streaming
- **LED Control**: Set individual or global controller LED colors
- **Vibration Feedback**: Rumble with configurable intensity and duration
- **Visual Effects**: Flash, pulse, rainbow, fade animations
- **Button Events**: Stream button press/release events
- **Battery Monitoring**: Low battery warnings with visual feedback

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Controller Manager Service                  │
├─────────────────────────────────────────────────────────────┤
│                      gRPC API Layer                          │
│  - StreamButtonEvents (bidirectional - buttons + LED ctrl)   │
│  - StreamGameplayData (bidirectional - motion + feedback)    │
│  - PlayControllerEffect                                      │
├─────────────────────────────────────────────────────────────┤
│            MultiplexerBackend (orchestrator)                  │
│  Centralized state: LED colors, rumble, effects per serial   │
│  Adapter assignment: serial → adapter (runtime-swappable)    │
│  LED keep-alive: 4s refresh cycle                            │
├─────────────────────────────────────────────────────────────┤
│               ControllerIOAdapter (sync I/O)                 │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│   │   Python    │  │    Rust    │  │    Mock     │         │
│   │  Adapter    │  │   Adapter  │  │   Adapter   │         │
│   │(gRPC →      │  │(gRPC →     │  │  (Testing)  │         │
│   │ python-hid) │  │ rust-hid)  │  │             │         │
│   └─────────────┘  └─────────────┘  └─────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

`MultiplexerBackend` routes per-controller calls through thin `ControllerIOAdapter` instances with centralized state. See [Backend layering](../../docs/specs/services.md#backend-layering) in the services spec for details.

## Configuration

Backend selection uses OpenFeature flags (performance domain):

| Setting | Source | Default | Description |
|---------|--------|---------|-------------|
| `controller_backend` | flagd (performance) | `python,rust` | Backend: `python`, `rust`, `mock`, or comma-separated |
| `mock_controller_count` | flagd (performance) | `4` | Number of mock controllers |
| `GRPC_PORT` | env var | `50052` | gRPC server port |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | env var | `http://localhost:4317` | OpenTelemetry endpoint |

### Backend Selection

The backend is selected via the `controller_backend` flag in flagd (`services/flagd/performance.json`).
If the flag is not set or flagd is unavailable, the default python backend is used.
Use `controller_backend=mock` for testing without hardware.

Multi-adapter mode: set `controller_backend=mock,python` to run mock and real controllers simultaneously.

## gRPC API

### Port
- **50052** (see docker-compose.yml)

### Proto Definition
See `proto/controller_manager.proto` for complete API specification.

### Key RPCs

```protobuf
// Real-time streaming (bidirectional)
rpc StreamButtonEvents(stream ButtonEventStreamControl) returns (stream ButtonEvent);
rpc StreamGameplayData(stream GameplayStreamControl) returns (stream GameplayDataUpdate);

// Feedback
rpc PlayControllerEffect(PlayControllerEffectRequest) returns (PlayControllerEffectResponse);
```

### Testing with grpcurl

```bash
# List available services
grpcurl -plaintext localhost:50052 list

# Play effect on controller
grpcurl -plaintext -d '{"serial": "CTRL001", "effect": 2, "color": {"r": 255, "g": 0, "b": 0}, "duration_ms": 500}' \
  localhost:50052 joustmania.ControllerManagerService/PlayControllerEffect
```

## Metrics

Metrics are pushed over OTLP (no `/metrics` endpoint; the prometheus_client server on :8000 was
removed in #1447). The full list is in [docs/specs/telemetry.md §6](../../docs/specs/telemetry.md#6-metrics).
Poll-loop health: `controller_poll_loop_lateness_seconds` (how late each 100 Hz poll started) and
`controller_poll_batch_duration_seconds`.

## Backend Interfaces

### ControllerBackend (high-level async)

Used by the servicer. `MultiplexerBackend` implements this:

```python
class ControllerBackend(ABC):
    async def initialize(self) -> bool
    async def scan_controllers(self) -> list[dict]
    async def get_controller_state(self, serial: str) -> dict | None
    async def set_led_color(self, serial: str, r: int, g: int, b: int) -> bool
    async def set_rumble(self, serial: str, intensity: int) -> bool
    def get_connected_controllers(self) -> list[str]
    async def shutdown()
```

### ControllerIOAdapter (thin sync I/O)

Used by `MultiplexerBackend`. Adapters handle raw hardware communication only:

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

`set_output()` combines LED + rumble in one call, matching HID output report reality.

## Performance Optimizations

### Polling Frequency

The discovery loop polls every tracked controller at a fixed 100 Hz (`controller_polling_target_hz` = 100,
`controller_polling_mode` = 1). The former adaptive 60 Hz / 10 Hz polling and its
`controller_adaptive_polling_*` gauges were removed (#1322); the loop still logs active vs idle
controller counts every 5 seconds.

### uvloop Event Loop

On Linux, the service uses [uvloop](https://github.com/MagicStack/uvloop) for 2-4x faster asyncio performance. This reduces input latency by 10-20%.

### LED Batch Updates (Phase 72)

LED updates are separated from controller polling and batched at 20Hz (every 50ms) instead of 60Hz. This reduces Bluetooth traffic while maintaining smooth visual feedback.

**Metrics:**
- `controller_led_batch_updates_total` - LED update cycles
- `controller_led_controllers_updated_per_batch` - Controllers updated per cycle

## Thread Safety

The service uses a `threading.RLock` (`state_lock`) to protect shared state accessed by both the discovery thread and gRPC handlers:
- `tracked_controllers`
- `controller_states`
- `button_states`

## Development

### Running Locally

```bash
# With real controllers (Linux)
uv run python -m services.controller_manager.server

# With mock controllers
MOCK_CONTROLLERS=true uv run python -m services.controller_manager.server
```

### Running Tests

```bash
# Run unit tests
uv run pytest services/controller_manager/tests/ -v

# Run with coverage
uv run pytest services/controller_manager/tests/ --cov=services.controller_manager
```

## Files

```
services/controller_manager/
├── server.py              # Entry point, gRPC server setup
├── servicer.py            # gRPC servicer implementation
├── backend.py             # ControllerBackend ABC
├── backend_factory.py     # Backend/adapter selection logic
├── bluetooth_backend.py   # Legacy BlueZ backend (standalone)
├── hidapi_backend.py      # Legacy HID backend (standalone)
├── mock_backend.py        # Legacy mock backend (standalone)
├── multiplexer/
│   ├── adapter.py         # ControllerIOAdapter ABC
│   ├── multiplexer_backend.py  # Orchestrator with centralized state
│   ├── hid_service_adapter.py  # HidServiceAdapter: shared gRPC HID-service client
│   ├── python_hid_adapter.py  # PythonHidAdapter (HidServiceAdapter → python-hid)
│   ├── rust_adapter.py    # RustServiceAdapter (HidServiceAdapter → rust-hid)
│   ├── mock_adapter.py    # MockAdapter (simulated I/O)
│   ├── bt_discovery.py    # CentralizedBTDiscovery
│   └── validation.py      # Backend combination validation
├── discovery_loop.py      # Discovery loop management
├── button_detector.py     # Button event detection
├── state_builder.py         # per-controller gameplay frame inputs (FrameInputs, #2353; no cache since #1675, renamed from state_cache.py in #1708)
├── effects_base.py        # Visual effect animations
├── feedback_manager.py    # LED/rumble feedback
├── event_publisher.py     # Event broadcasting
├── name_manager.py        # Controller naming
├── metrics.py             # Prometheus/OTEL metrics
├── Dockerfile
├── pyproject.toml
└── tests/                 # Unit tests
```

## See Also

- [Main Project Documentation](../../README.md)
- [Services spec](../../docs/specs/services.md)
- [Proto Definitions](../../proto/controller_manager.proto)
