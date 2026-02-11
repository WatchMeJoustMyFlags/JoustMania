# JoustMania Architecture

This document provides a comprehensive overview of the JoustMania microservices architecture.

## Overview

JoustMania is a party game system for PS Move controllers, built as a collection of microservices communicating via gRPC. The architecture supports:

- Real-time controller input processing (1000Hz hardware polling)
- Multiple game modes with different rules
- Web-based administration
- Distributed tracing and observability

## Service Diagram

```
                           ┌──────────────┐
                           │  Dashboard   │ :8080
                           │  (Dashboard) │
                           └──────┬───────┘
                                  │ HTTP + gRPC
    ┌─────────────────────────────┼─────────────────────────────┐
    │                             │                             │
    ▼                             ▼                             ▼
┌──────────────┐          ┌──────────────┐          ┌──────────────────┐
│    flagd     │          │    Menu      │─────────►│ Game Coordinator │
│   :8013      │          │   :50054     │          │     :50053       │
└──────────────┘          └──────┬───────┘          └────────┬─────────┘
                                 │                           │
                                 │ StreamGameEvents          │
                                 │ (start_config)            │
                                 ▼                           │
                         ┌──────────────┐                    │
                         │    Audio     │◄───────────────────┤
                         │   :50056     │                    │
                         └──────────────┘                    │
                                                             │
                         ┌──────────────────┐                │
                         │ Controller Mgr   │◄───────────────┘
                         │     :50052       │
                         └────────┬─────────┘
                                  │
                           USB/Bluetooth
                                  │
                         ┌────────┴────────┐
                         │  PS Move        │
                         │  Controllers    │
                         └─────────────────┘
```

## Services

### Controller Manager Service (Port 50052)

**Purpose**: PS Move controller hardware management

**Responsibilities**:
- Hardware discovery (USB/Bluetooth)
- Controller pairing via BlueZ
- Real-time state streaming (1000Hz polling → 60Hz stream)
- LED control and visual effects
- Vibration feedback
- Button event detection

**Key RPCs**:
| RPC | Type | Description |
|-----|------|-------------|
| `StreamButtonEvents` | Bidirectional | Button events + LED control commands |
| `StreamGameplayData` | Bidirectional | Filtered motion data + feedback commands |
| `PlayControllerEffect` | Unary | Trigger visual effect (flash, pulse, rainbow) |

**Backends**:
- `bluetooth` - Linux BlueZ (production)
- `windows` - psmoveapi (Windows)
- `mock` - Simulated controllers (testing)

**Special Features**:
- Adaptive polling: 60Hz active, 10Hz idle
- LED batch updates at 20Hz
- Effect priority system (cancellable vs non-cancellable)

**Dependencies**: None (foundational service)

---

### Game Coordinator Service (Port 50053)

**Purpose**: Game lifecycle and rules management

**Responsibilities**:
- Start/stop games
- Game state machine (IDLE → STARTING → RUNNING → ENDING → ENDED)
- Death detection based on movement thresholds
- Game event streaming
- Support for 13 game modes

**Game Modes**:
- JoustFFA, JoustTeams, JoustRandomTeams
- Werewolf, Traitor, Zombies
- Commander, Swapper
- Tournament, FightClub
- NonStop, Ninja
- Random

**Key RPCs**:
| RPC | Type | Description |
|-----|------|-------------|
| `StreamGameEvents` | Server Stream | Start game (if start_config provided) and stream lifecycle events |
| `ForceEndGame` | Unary | Force end current game |
| `GetGameState` | Unary | Query current game state (for testing/observability) |

**Game Configuration**: Games receive typed configuration via `StartGameConfig` proto:
- `game_name`: Game mode identifier
- `players`: List of players with serial, team, alive status
- `sensitivity`: Movement sensitivity level (0-4)
- `game_config`: Mode-specific settings via `oneof` (see [Game Configuration](#game-configuration))

**Dependencies**: ControllerManager, Audio

---

### Menu Service (Port 50054)

**Purpose**: Menu UI and game selection

**Responsibilities**:
- Menu state management
- Process controller button inputs
- Game mode selection cycling
- Team assignment
- Admin mode controls
- LED feedback based on state
- Auto-start when all players ready
- Local game settings storage (`state_manager.game_settings`)

**Key RPCs**:
| RPC | Type | Description |
|-----|------|-------------|
| `StartMenu` | Unary | Start menu mode |
| `StopMenu` | Unary | Stop menu mode |
| `ProcessInput` | Unary | Process button input |
| `StreamMenuEvents` | Server Stream | Menu state change events |

**Admin Mode**: Press all 4 face buttons simultaneously
- White LED indicator
- 60 second timeout
- Move button: Cycle through options
- Trigger/Cross: Increase/decrease value
- Configurable options: sensitivity, num_teams, random_assignment, nonstop_time_limit,
  invincibility, fight_club_min_rounds, werewolf_reveal_time, force_all_start

**Dependencies**: ControllerManager, GameCoordinator

---

### Audio Service (Port 50056)

**Purpose**: Audio playback and mixing

**Responsibilities**:
- Play sound effects with priorities
- Play background music with tempo control
- Audio device management

**Priority Levels**:
| Priority | Value | Use Case |
|----------|-------|----------|
| LOW | 0 | Background effects |
| MEDIUM | 1 | Normal game sounds |
| HIGH | 2 | Important events |
| CRITICAL | 3 | System announcements |

**Key RPCs**:
| RPC | Type | Description |
|-----|------|-------------|
| `PlaySound` | Unary | Play sound effect |
| `PlayMusic` | Unary | Start background music |
| `StopMusic` | Unary | Stop music |
| `ChangeTempo` | Unary | Adjust music speed |
| `SetVolume` | Unary | Set volume level |

**Dependencies**: None (foundational)

---

### Dashboard Service (Port 8080)

**Purpose**: Unified web interface and reverse proxy

**Components**:
- Caddy reverse proxy for unified access
- Web dashboard for controller visualization
- gRPC-web bridge for browser clients

**Routes** (via reverse proxy):
| Route | Target | Description |
|-------|--------|-------------|
| `/` | Dashboard | Main web interface |
| `/jaeger/` | Jaeger:16686 | Distributed tracing UI |
| `/grafana/` | Grafana:3000 | Metrics dashboards |
| `/prometheus/` | Prometheus:9090 | Metrics API |

**Dependencies**: All observability services

---

## Communication Patterns

### Protocol: gRPC with Protocol Buffers

JoustMania uses gRPC for all inter-service communication:
- Binary protocol (3-10x faster than REST/JSON)
- Strong typing with Protocol Buffers v3
- HTTP/2 multiplexing
- Bidirectional streaming support

### Service Discovery

Services discover each other via Docker Compose DNS:
```
controller-manager:50052
game-coordinator:50053
menu:50054
audio:50056
```

### Streaming Patterns

**Server Streaming** (Service → Client):
- `StreamGameplayData` - Controller motion
- `StreamGameEvents` - Game lifecycle
- `StreamMenuEvents` - Menu state

**Bidirectional Streaming** (Both directions):
- `StreamButtonEvents` - Buttons ↔ LED commands
- `StreamGameplayData` - Motion ↔ Feedback

---

## Data Flows

### Controller Connection

```
1. PS Move hardware (1000Hz) → Controller Manager
2. Controller Manager updates internal state
3. Menu/Game subscribes via StreamButtonEvents
4. Controller Manager sends button events
5. Menu/Game sends LED commands back
6. Controller Manager applies LED to hardware
```

### Game Start

```
1. Menu detects all controllers ready (≥2 players)
2. Menu builds typed StartGameConfig from state_manager.game_settings:
   - game_name, players list, sensitivity
   - Mode-specific config (e.g., TeamsConfig, WerewolfConfig)
3. Menu calls GameCoordinator.StreamGameEvents(start_config)
4. GameCoordinator validates and initializes game instance with typed config
5. GameCoordinator streams back game lifecycle events:
   - game_start → countdown → game_started → player_death... → game_end
6. Menu monitors events via separate subscription
7. Game runs until win condition
8. Menu receives "game_ended", resets to lobby
```

## Game Configuration

Game-specific settings are stored locally in Menu service's `state_manager.game_settings`
and passed to Game Coordinator via typed proto messages when starting games.

### Local Game Settings (Menu)

| Setting | Type | Range | Description |
|---------|------|-------|-------------|
| `sensitivity` | int | 0-4 | Movement threshold (0=Ultra Slow, 4=Ultra Fast) |
| `num_teams` | int | 2-6 | Number of teams (Teams, RandomTeams, Traitor) |
| `random_assignment` | bool | - | Random team assignment in Teams mode |
| `nonstop_time_limit` | int | 0, 60-300 | Time limit in seconds (0=unlimited) |
| `invincibility` | float | 2.0-8.0 | Spawn protection seconds (Tournament, FightClub) |
| `fight_club_min_rounds` | int | 5-20 | Minimum rounds before game can end |
| `werewolf_reveal_time` | float | 20.0-60.0 | Seconds before werewolves are revealed |
| `force_all_start` | bool | - | Force start with all connected controllers |

### Typed Config Messages (Proto)

Games receive mode-specific configuration via `oneof game_config` in `StartGameConfig`:

| Game Mode | Config Message | Fields |
|-----------|---------------|--------|
| JoustFFA | `FFAConfig` | (none - uses base settings) |
| JoustTeams | `TeamsConfig` | `num_teams`, `random_assignment` |
| JoustRandomTeams | `TeamsConfig` | `num_teams`, `random_assignment` |
| Nonstop | `NonstopConfig` | `time_limit_seconds` |
| Tournament | `TournamentConfig` | `invincibility_seconds` |
| FightClub | `FightClubConfig` | `invincibility_seconds`, `min_rounds` |
| Werewolf | `WerewolfConfig` | `reveal_time_seconds` |
| Zombies | `ZombieConfig` | (none - uses base settings) |
| Swapper | `SwapperConfig` | (none - uses base settings) |
| Traitor | `TraitorConfig` | `num_teams` |

### Admin Mode Configuration

Settings are configured via admin mode (hold all 4 face buttons):
1. **Move button**: Cycle through options (LED shows option color)
2. **Trigger**: Increase value
3. **Cross**: Decrease value
4. Settings persist in memory until service restart

---

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CONTROLLER_BACKEND` | Controller backend type | `bluetooth` |
| `MOCK_CONTROLLER_COUNT` | Simulated controllers | `4` |
| `AUDIO_MOCK_MODE` | Silent audio mode | `false` |
| `LOG_LEVEL` | Logging verbosity | `INFO` |

### Docker Compose Variants

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Full stack with observability |
| `docker-compose.dynatrace.yml` | Dynatrace parallel export (see [Dynatrace docs](DYNATRACE.md)) |
| `docker-compose.lite.yml` | Minimal stack (no observability) |
| `docker-compose.test.yml` | Integration testing |
| `docker-compose.ci.yml` | CI builds |

### Persistence

- `~/.psmoveapi/` - Controller calibration (Linux)
- Feature flags managed via flagd (JSON flag files in `services/flagd/`)

---

## Observability

### Stack

All observability tools are accessed through the unified dashboard reverse proxy at `http://localhost:8080/`:

| Component | Purpose | Dashboard Path | Internal Port |
|-----------|---------|----------------|---------------|
| Dashboard | Unified entry point | `/` | 80 |
| Jaeger | Distributed tracing | `/jaeger/` | 16686 |
| Prometheus | Metrics collection | `/prometheus/` | 9090 |
| Grafana | Visualization | `/grafana/` | 3000 |
| Loki | Log aggregation | `/loki/` | 3100 |

**Note:** The dashboard uses Caddy as a reverse proxy to provide a single entry point for all services. See `docs/CADDY_PROXY.md` for configuration details.

### Tracing

- OpenTelemetry SDK with Jaeger exporter
- Automatic gRPC instrumentation
- W3C Trace Context propagation
- Manual spans for critical operations

### Metrics

Each service exposes Prometheus metrics at `/metrics`:
- Request counts and latencies
- Active stream counts
- Controller counts
- Game statistics

### Dynatrace (Optional)

JoustMania supports optional parallel export of all telemetry (traces, metrics, logs) to Dynatrace via OTLP HTTP. This is enabled via a Docker Compose override file that swaps the OTEL Collector config to add Dynatrace-specific pipelines.

The local stack continues to operate identically — Dynatrace receives a parallel copy of the data with delta temporality conversion for counters.

See [Dynatrace Integration](DYNATRACE.md) for setup instructions.

---

## Build & Deployment

### Build Commands

```bash
make builders   # Build base images (~15min on Pi)
make images     # Build all service images
make up         # Start full stack
make up-mock    # Start with mock controllers
make test       # Run integration tests
make lint       # Run linting
```

### Container Privileges

| Service | Privileged | Reason |
|---------|------------|--------|
| Controller Manager | Yes | Bluetooth/USB access |
| Audio | Yes | Audio device access |
| Others | No | Standard containers |

---

## Shared Libraries

Located in `/lib/`:

| Module | Purpose |
|--------|---------|
| `grpc_tracing.py` | OpenTelemetry gRPC interceptor |
| `types.py` | Shared type definitions |
| `colors.py` | Color constants |
| `controller_constants.py` | Controller constants |
| `telemetry.py` | Telemetry helpers |

---

## Proto Files

Located in `/proto/`:

| File | Service |
|------|---------|
| `controller_manager.proto` | ControllerManagerService |
| `game_coordinator.proto` | GameCoordinatorService |
| `menu.proto` | MenuService |
| `audio.proto` | AudioService |

Regenerate after changes:
```bash
make protos
```
