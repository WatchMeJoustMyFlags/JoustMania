# Test Case: Host Tunes the Game in Admin Mode and Force-Starts

## Overview

**ID:** TC-005  
**Goal:** The host enters admin mode, changes the sensitivity and the game mode, and force-starts a game that uses the new settings.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-012](../use_cases/UC-012.md) main, A1; [UC-011](../use_cases/UC-011.md) main; [UC-014](../use_cases/UC-014.md) main; [UC-010](../use_cases/UC-010.md) main

## Roles

- Host (holds one controller and uses the admin combo)
- Players (connected controllers that join the forced start)

## Preconditions

- The integration stack is up with the menu running; mock controllers `MOCK0001` (host) … `MOCK0004` connected.
- `sensitivity` in the active `game.json` is `medium`; `force_all_start` is `on` (the `admin_flags` fixture of `tests/integration/test_admin_flow.py` snapshots and restores the flag files).

## Flow

| Step | Name                    | Description                                                                                          | Test Data                                   | Use Case                            |
|------|-------------------------|------------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Enter admin mode        | Host presses Cross, Circle, Square and Triangle together; the controller flashes and stays white     | `MOCK0001`                                  | [UC-012](../use_cases/UC-012.md)    |
| 2    | Change sensitivity      | Host presses Circle; the system steps `sensitivity` one level up and persists it                     | level 2 (`medium`) → level 3                | [UC-012](../use_cases/UC-012.md)    |
| 3    | Verify persisted        | flagd serves the level-3 variant as the `sensitivity` default                                             | -                                           | -                                   |
| 4    | Change the game mode    | Host presses Cross; the system advances to the next mode and persists `current_game`                 | `JoustFFA` → `JoustTeams`                   | [UC-011](../use_cases/UC-011.md)    |
| 5    | Force-start             | Host holds the trigger for 3 s; the system builds the roster from all connected controllers and starts | trigger hold 3 s                          | [UC-014](../use_cases/UC-014.md)    |
| 6    | Verify the game starts  | The menu publishes `game_requested` (source `admin_force_start`) and the game starts in the selected mode | -                                       | [UC-010](../use_cases/UC-010.md)    |
| 7    | Verify admin left       | The host's controller is no longer white                                                             | -                                           | -                                   |

## Validation

1. **Settings reach the game**: the started game's mode is the one selected in step 4.
2. **Flags persisted**: `game.json` and `user.json` hold the new `sensitivity` and `current_game` default variants after the game.

## Postconditions

- `sensitivity` and `current_game` are changed in the active flag files; the `admin_flags` fixture restores them byte for byte.
- One started game that the suite's `coordinator_idle` fixture ends.

## Automation

- `tests/integration/test_admin_flow.py`: `test_combo_entry_lights_admin_white` (step 1), `test_sensitivity_change_persists` (steps 2 and 3), `test_option_cycle_and_value_change_persists` (UC-012 main steps 3 to 6), `test_force_start_requests_game` (steps 5 and 6), `test_force_start_respects_selected_game_mode` (steps 4 to 6), `test_ps_exit_restores_lobby_color` (UC-012 steps 7 and 8).
- **Partly automated**: the steps run as separate tests, not as one chained journey; in CI the trigger hold is about 1.5 s (the force-start duration is shortened by flag), not 3 s. Step 6 asserts only that the coordinator start path is reached (`game_started` or `game_start_cancelled`); step 7 and Validations 1 and 2 are not asserted. Gaps (planned, #1359): mode announcement and colour pulse (UC-011 step 3), the early release of the trigger (UC-014 A1) and the admin timeout (UC-012 A4) are not covered end to end.
