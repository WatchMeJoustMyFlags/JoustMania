# Test Case: FFA Game Night on Real Controllers

## Overview

**ID:** TC-001  
**Goal:** Players pair two new PS Move controllers over USB, connect them, ready up and play one Joust Free-for-All game to a single winner, hearing the game audio, and the lobby comes back.  
**Priority:** Critical  
**Status:** Draft  
**Use cases:** [UC-001](../use_cases/UC-001.md) main; [UC-002](../use_cases/UC-002.md) main, A1, A4; [UC-010](../use_cases/UC-010.md) main; [UC-020](../use_cases/UC-020.md) main; [UC-030](../use_cases/UC-030.md) main; [UC-031](../use_cases/UC-031.md) main

## Roles

- Player (pairs, connects, readies up and plays)
- Second player (readies up and plays)

## Preconditions

- The full stack runs on the Raspberry Pi with at least one Bluetooth adapter (`./scripts/setup/joustmania-compose.sh up -d`: `-f docker-compose.yml` plus `.env` and `.env.local`; never a bare `docker compose`, which loads the dev override, see [hardware-setup-guide.md](../hardware-setup-guide.md)).
- Two PS Move controllers that this Pi has not paired in the current session, charged above 40 %.
- `current_game` in `services/flagd/user.json` is `JoustFFA`; `play_audio` is on; `sensitivity` (`services/flagd/game.json`) is `medium`.

## Flow

| Step | Name                 | Description                                                                                                   | Test Data                      | Use Case                            |
|------|----------------------|---------------------------------------------------------------------------------------------------------------|--------------------------------|-------------------------------------|
| 1    | Pair over USB        | Player plugs each controller into a USB port; the system pairs it to the least-loaded adapter and logs that it can be unplugged | controllers A and B            | [UC-001](../use_cases/UC-001.md)    |
| 2    | Connect              | Player unplugs each controller and presses the PS button; the system names it and lights it dim in the FFA lobby colour | PS button on A and B           | [UC-002](../use_cases/UC-002.md)    |
| 3    | Verify lobby         | Both controllers are CONNECTED with a dim LED; lobby music plays                                              | -                              | -                                   |
| 4    | Ready up             | Both players press the trigger; each gets a beep and a bright LED; the system starts the game when both are READY | trigger on A, then on B        | [UC-010](../use_cases/UC-010.md)    |
| 5    | Verify countdown     | Lobby music stops, three colour phases with beeps play, then the start sound; each player shows a unique colour | -                              | -                                   |
| 6    | Play and eliminate   | Player B shakes the controller hard; the system warns (white flash, rumble), then eliminates B with an explosion and the red death effect | shake B above the death threshold | [UC-030](../use_cases/UC-030.md) |
| 7    | Declare the winner   | The system declares A the winner, shows the rainbow on A for 3 s and plays "congratulations"                  | -                              | [UC-020](../use_cases/UC-020.md)    |
| 8    | Hear the game audio  | During steps 5 to 7 the game music alternated between 1.0× and 1.3× and effects played over it                | -                              | [UC-031](../use_cases/UC-031.md)    |
| 9    | Verify lobby returns | Both controllers return to CONNECTED (dim, un-readied) and lobby music plays again                            | -                              | -                                   |
| 10   | Disconnect           | Player turns controller B off; the system removes it from the lobby                                          | PS button hold on B            | [UC-002](../use_cases/UC-002.md)    |

## Validation

1. **One game, one winner**: the game's event stream has `player_death` for B, `game_winner` for A, then `game_ended`; in Jaeger (`/jaeger/`, service `game-coordinator`) the game has one root span `Joust Free-for-All`, and A's player span carries the `victory` event.
2. **Pairing kept**: turning controller A off and on again reconnects it with the PS button alone (no USB step, UC-002 A4) under the same name.
3. **Lobby restored**: A is CONNECTED with the FFA lobby colour at 30 % brightness; B is gone from the lobby and `controller_connected` for B is 0.

## Postconditions

- Controllers A and B are paired to the Pi's adapters and stored in the controller names file.
- Controller A stays connected in the lobby; controller B is disconnected.
- No flag file is changed.

## Automation

- **Manual** (needs real Bluetooth hardware and USB). The mock-controller equivalent of steps 2 to 9 is [TC-002](TC-002-ffa-mock-game-to-winner.md).
- Unit tests cover parts: pairing (`services/pairing_daemon/tests/`), death detection (`services/game_coordinator/tests/`).
- Gaps: no automated check of USB pairing (UC-001) or of audible feedback (UC-031) on hardware. Pairing logic is unit-tested in `services/pairing_daemon/tests/` (#1400, closing #1359); the hardware check stays manual. Audible feedback is tracked by #1362 (UC-031).
