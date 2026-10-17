# Test Case: Player Disconnects During an FFA Game

## Overview

**ID:** TC-003  
**Goal:** In a three-player Free-for-All game one controller disconnects and another dies, and the game ends with the last player as winner without waiting for motion from the disconnected controller.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-003](../use_cases/UC-003.md) main; [UC-010](../use_cases/UC-010.md) main; [UC-020](../use_cases/UC-020.md) A2; [UC-030](../use_cases/UC-030.md) A5; [UC-002](../use_cases/UC-002.md) A1

## Roles

- Operator (drives three mock controllers and removes one mid-game)

## Preconditions

- The integration stack is up (session fixture `docker_compose`), `current_game` is `JoustFFA`, no game running.

## Flow

| Step | Name                     | Description                                                                                          | Test Data                          | Use Case                            |
|------|--------------------------|------------------------------------------------------------------------------------------------------|------------------------------------|-------------------------------------|
| 1    | Add mock controllers     | Operator adds three mock controllers                                                                 | `MOCK0001`, `MOCK0002`, `MOCK0003` | [UC-003](../use_cases/UC-003.md)    |
| 2    | Start FFA                | Operator readies up all three; the game starts                                                       | trigger on all three               | [UC-010](../use_cases/UC-010.md)    |
| 3    | Disconnect a controller  | Operator removes `MOCK0002` while the game runs                                                      | `MOCK0002`                         | [UC-002](../use_cases/UC-002.md)    |
| 4    | Verify elimination       | The system eliminates `MOCK0002` through the death path: `player_death` with 2 players left, and `player_disconnected` on the player span | -        | [UC-030](../use_cases/UC-030.md)    |
| 5    | Verify game goes on      | The game keeps running with `MOCK0001` and `MOCK0003`                                                | -                                  | [UC-020](../use_cases/UC-020.md)    |
| 6    | Eliminate another player | Operator simulates a death on `MOCK0001`                                                             | `MOCK0001`                         | [UC-030](../use_cases/UC-030.md)    |
| 7    | Verify winner            | `game_winner` names `MOCK0003`, then `game_ended`                                                    | winner `MOCK0003`                  | [UC-020](../use_cases/UC-020.md)    |
| 8    | Verify lobby             | The lobby holds `MOCK0001` and `MOCK0003` only; `controller_connected{serial="MOCK0002"}` is 0       | -                                  | [UC-002](../use_cases/UC-002.md)    |

## Validation

1. **Disconnect is a death**: the game's event stream has exactly two `player_death` events (`MOCK0002`, `MOCK0001`) and one `game_winner` (`MOCK0003`).
2. **No ghost in the lobby**: `MOCK0002` is absent from the menu's controller list after the game.

## Postconditions

- Mock controllers `MOCK0001` and `MOCK0003` stay connected; `MOCK0002` is removed.
- One ended game session.

## Automation

- **Planned** (#1360, FFA disconnect journey). Unit coverage exists in `services/game_coordinator/tests/test_disconnect.py` and `services/game_coordinator/tests/test_ffa_uc020.py` (UC-020 A2, #1396).
