# Test Case: Host Tunes the Game in Admin Mode and Force-Starts

## Overview

**ID:** TC-005  
**Goal:** The host enters admin mode, changes the sensitivity and the game mode, and force-starts a game that uses the new settings.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-012](../use_cases/UC-012.md) main, A1; [UC-011](../use_cases/UC-011.md) main; [UC-014](../use_cases/UC-014.md) main; [UC-010](../use_cases/UC-010.md) main; [UC-053](../use_cases/UC-053.md) A2

## Roles

- Host (holds one controller and uses the admin combo)
- Players (connected controllers that join the forced start)

## Preconditions

- The integration stack is up with the menu running; mock controllers `MOCK0001` (host) … `MOCK0004` connected.
- `sensitivity` serves `medium` (the baseline in `services/flagd/ci/game.json`); `force_all_start` is `on` (a stack overlay on the flag service). The test runs on its own fresh flag plane, so the persisted admin values go away with it; the flag files are never written.

## Flow

| Step | Name                    | Description                                                                                          | Test Data                                   | Use Case                            |
|------|-------------------------|------------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Enter admin mode        | Host presses Cross, Circle, Square and Triangle together; the controller flashes and stays white     | `MOCK0001`                                  | [UC-012](../use_cases/UC-012.md)    |
| 2    | Change sensitivity      | Host presses Circle; the menu sends the next level to the flag service as a persisted admin value     | level 2 (`medium`) → level 3                | [UC-012](../use_cases/UC-012.md)    |
| 3    | Verify persisted        | flagd serves the level-3 variant as the `sensitivity` default                                             | -                                           | -                                   |
| 4    | Change the game mode    | Host presses Cross; the system advances to the next mode and persists `current_game` (admin value)    | `JoustFFA` → `JoustTeams`                   | [UC-011](../use_cases/UC-011.md)    |
| 5    | Force-start             | Host holds the trigger for 3 s; the system builds the roster from all connected controllers and starts | trigger hold 3 s                          | [UC-014](../use_cases/UC-014.md)    |
| 6    | Verify the game starts  | The menu publishes `game_requested` (source `admin_force_start`) and the game starts in the selected mode | -                                       | [UC-010](../use_cases/UC-010.md)    |
| 7    | Verify admin left       | The host's controller is no longer white                                                             | -                                           | -                                   |

## Validation

1. **Settings reach the game**: the started game's mode is the one selected in step 4, and it runs at the sensitivity level set in step 2 (its `game_sensitivity` gauge, UC-012 step 6).
2. **Flags persisted**: the flag service holds `sensitivity` and `current_game` as persisted admin values (`admin_value` in `GET /game/sensitivity`, `GET /user/current_game`), UC-053 A2; the baseline flag files are byte-for-byte unchanged (UC-012 Post-S-1).

## Postconditions

- `sensitivity` and `current_game` are persisted admin values; they live only in the test's flag plane, which is discarded with it.
- One started game, which the test force-ends at teardown.

## Automation

**Tier:** T2 service (`tests/service/`, `make test-service`): menu, game coordinator and controller manager (mock controllers) from source at HEAD, flag service and one flagd, fake audio, OTLP to the stack's sink (`LOBBY` composition with `otlp=True`).

- `tests/service/test_tc005_admin_journey.py::test_host_tunes_and_force_starts` implements steps 1 to 7 and Validations 1 and 2 as one journey (moved from the docker-compose suite in #1562). Along the way it asserts the partial combo (three face buttons do not enter admin), UC-012 main steps 3 to 5 (Move selects `num_teams`, Select increments it) and steps 7, 8 (PS leaves admin white; the host then re-enters admin for the force start), the UC-011 step 3 mode announcement on the fake audio service, the step 5 roster (every connected controller) and, in step 7, a game colour on the host (lit, not admin white).
- In the test the trigger hold is about 1.5 s (the force-start duration is shortened by flag), not 3 s.
- Gaps (unit-tested only, #1400): the colour pulse of UC-011 step 3, the early release of the trigger (UC-014 A1) and the admin timeout (UC-012 A4) are not covered end to end.
