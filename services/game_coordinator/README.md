# Game Coordinator Service

**Part of JoustMania Microservices Architecture**

## Overview

The Game Coordinator Service manages the game lifecycle and orchestrates gameplay across all connected controllers. It handles game mode selection, player management, death detection, scoring, and game state transitions.

## Quick Reference

| Property | Value |
|----------|-------|
| **Port** | 50053 |
| **Proto** | `proto/game_coordinator.proto` |
| **Container** | `joustmania-game-coordinator` |

## gRPC API

There is no `StartGame` RPC: a game starts through `StreamGameEvents` with `start_config` set (below).
The RPCs are `StreamGameEvents`, `ForceEndGame`, `GetGameState` and `ListGames`.

### GetGameState
Gets current game state and players (`ListGames` lists every live session, primary and shadow).

```bash
grpcurl -plaintext localhost:50053 \
  joustmania.game_coordinator.GameCoordinatorService/GetGameState
```

### ForceEndGame
Forces the current game to end.

```bash
grpcurl -plaintext -d '{"reason": "admin_requested"}' \
  localhost:50053 joustmania.game_coordinator.GameCoordinatorService/ForceEndGame
```

### StreamGameEvents
Streams real-time game events. With `start_config` set it first starts a new game (the menu does this);
with it empty it subscribes to the current or upcoming game.

```bash
grpcurl -plaintext -d '{"start_config": {"game_name": "JoustFFA", "sensitivity": 2}}' \
  localhost:50053 joustmania.game_coordinator.GameCoordinatorService/StreamGameEvents
```

```bash
grpcurl -plaintext localhost:50053 \
  joustmania.game_coordinator.GameCoordinatorService/StreamGameEvents
```

## Game States

| State | Description |
|-------|-------------|
| `IDLE` | No game running, waiting for start |
| `STARTING` | Session created, game initializing; becomes `RUNNING` on the `game_started` event, which is published before the countdown |
| `RUNNING` | Game in progress (including the countdown) |
| `ENDING` | Reserved, never set (a session goes from `RUNNING` straight to `ENDED`) |
| `ENDED` | Game complete, showing results |

## Supported Game Modes

| Mode | Description |
|------|-------------|
| `JoustFFA` | Free-for-all joust - last player standing wins |
| `JoustTeams` | Team-based joust |
| `JoustRandomTeams` | Random team assignment |
| `Traitor` | Hidden traitors among the players |
| `Werewolf` | Hidden role deduction game |
| `Zombies` | Infection-style game |
| `Swapper` | Role-swapping variant |
| `FightClub` | Defender versus challengers, round by round |
| `Tournament` | Bracket-style elimination |
| `NonStop` | Continuous respawn mode |

`Commander`, `Ninja` and `Random` exist only as `Games` enum names; they are not registered in `_GAME_CLASSES` and a start request answers `game_error` "not implemented" (D-G1 in `docs/specs-inventory.md`).

## Architecture

```
┌──────────────────────┐
│   Menu Service       │──── StreamGameEvents(start_config)
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐     ┌─────────────────┐
│  Game Coordinator    │◄────│ Controller Mgr  │
│  (port 50053)        │     │ (motion data)   │
└──────────┬───────────┘     └─────────────────┘
           │
           ├──► Audio Service (sounds/music)
           └──► Controller Mgr (LED feedback)
```

## Distributed Tracing

The Game Coordinator creates comprehensive traces for:
- Per-player lifecycle spans
- Per-team spans (team games)
- Death detection events
- Game phase transitions

View traces in Jaeger: `http://localhost:16686`

## Development

```bash
# Run locally
cd services/game_coordinator
python server.py

# Run tests
pytest tests/
```

## See Also

- [Services spec](../../docs/specs/services.md) - System architecture
- [Proto Definition](../../proto/game_coordinator.proto) - Full API specification
- [Distributed Tracing](./DISTRIBUTED_TRACING.md) - Tracing implementation
- [Development Guide](../../docs/DEVELOPMENT.md) - Development workflow
