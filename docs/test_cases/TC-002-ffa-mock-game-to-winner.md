# Test Case: FFA Game to a Winner on Mock Controllers

## Overview

**ID:** TC-002  
**Goal:** An operator adds four mock controllers, selects Joust Free-for-All, readies everyone up and eliminates three players, and the game ends with one winner and the lobby restored.  
**Priority:** Critical  
**Status:** Draft  
**Use cases:** [UC-003](../use_cases/UC-003.md) main; [UC-002](../use_cases/UC-002.md) main; [UC-011](../use_cases/UC-011.md) A1; [UC-010](../use_cases/UC-010.md) main; [UC-020](../use_cases/UC-020.md) main; [UC-030](../use_cases/UC-030.md) main

## Roles

- Operator (drives the mock controllers through the mock control API and the menu)

## Preconditions

- The integration stack is up (`make test`, session fixture `docker_compose` in `tests/integration/conftest.py`), mock backend selected, no game running.
- No other mock controllers than the four this journey adds are unreserved.

## Flow

| Step | Name                  | Description                                                                                         | Test Data                                  | Use Case                            |
|------|-----------------------|-----------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Add mock controllers  | Operator adds four mock controllers; the system connects them like hardware                         | count 4 → `MOCK0001` … `MOCK0004`          | [UC-003](../use_cases/UC-003.md)    |
| 2    | Verify lobby          | Every mock controller is CONNECTED in the lobby                                                     | -                                          | [UC-002](../use_cases/UC-002.md)    |
| 3    | Select FFA            | Operator selects the mode through the web command                                                   | `select_game`, `JoustFFA`                  | [UC-011](../use_cases/UC-011.md)    |
| 4    | Ready up              | Operator presses the trigger on every mock controller; the game starts when all four are READY      | trigger on `MOCK0001` … `MOCK0004`         | [UC-010](../use_cases/UC-010.md)    |
| 5    | Verify game colours   | `game_started` is published and every controller shows a non-black game colour                     | -                                          | -                                   |
| 6    | Eliminate players     | Operator simulates a death on three controllers one after the other                                 | `MOCK0001`, `MOCK0002`, `MOCK0003`, 0.1 s apart | [UC-030](../use_cases/UC-030.md) |
| 7    | Verify game end       | The last player wins and `game_ended` is published within 15 s                                      | winner `MOCK0004`                          | [UC-020](../use_cases/UC-020.md)    |
| 8    | Verify lobby restored | Every controller returns to the FFA lobby colour (orange at 30 % brightness) within 10 s            | -                                          | [UC-010](../use_cases/UC-010.md)    |

## Validation

1. **One terminal event**: exactly one of `game_ended`, `game_force_ended`, `game_error` arrives for the game, and it is `game_ended`.
2. **Lobby colours restored**: the LED colour stream shows each controller going from its game colour back to the FFA lobby colour, never stuck at black.

## Postconditions

- Four mock controllers `MOCK0001` … `MOCK0004` stay connected and CONNECTED (un-readied); the session fixture removes them at teardown.
- One ended game session; no flag file is changed.

## Automation

- **Partly automated**: `tests/integration/test_full_game_lifecycle.py::test_full_game_lifecycle[JoustFFA]` drives steps 1 to 8.
- Gaps (planned, #1360): step 5 checks only that every LED is non-black, not a unique game colour per player; step 7 and Validation 1 accept any of `game_ended`, `game_force_ended`, `game_error`, so a force-ended or failed game passes; the test does not assert `players_initialized`, one `player_death` per eliminated player with the players-left count, `game_winner` naming `MOCK0004`, the winner rainbow, or `player_analytics`; it uses simulated deaths, so the warning effect (UC-030 step 7) is not observed. Add `@pytest.mark.uc(..., tc="TC-002")` markers when the test is retagged (#1356).
