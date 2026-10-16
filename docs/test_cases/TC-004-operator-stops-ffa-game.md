# Test Case: Operator Stops an FFA Game

## Overview

**ID:** TC-004  
**Goal:** An operator starts a Free-for-All game from the dashboard, stops it with a reason, and the game ends without a winner, the lobby returns and a new game can start at once.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-010](../use_cases/UC-010.md) A5; [UC-020](../use_cases/UC-020.md) A4; [UC-027](../use_cases/UC-027.md) main, A1, A2

## Roles

- Operator (uses the dashboard or the game coordinator's force-end call)

## Preconditions

- The integration stack is up; four mock controllers `MOCK0001` … `MOCK0004` are connected and READY (see [TC-002](TC-002-ffa-mock-game-to-winner.md) steps 1 to 4 without the last trigger press); `current_game` is `JoustFFA`.

## Flow

| Step | Name                       | Description                                                                                  | Test Data                                 | Use Case                            |
|------|----------------------------|----------------------------------------------------------------------------------------------|-------------------------------------------|-------------------------------------|
| 1    | Start from the dashboard   | Operator presses Start; the system starts FFA with the controllers that are READY now        | `start_game`                              | [UC-010](../use_cases/UC-010.md)    |
| 2    | Verify game runs           | `game_started` is published with a game id                                                   | game id `G1` (captured)                   | -                                   |
| 3    | Stop with an unknown id    | Operator stops a game id that does not exist; the call fails and the game keeps running      | `game_unknown`, reason `tc004`            | [UC-027](../use_cases/UC-027.md)    |
| 4    | Stop the game              | Operator presses Stop with a reason; the system ends `G1` without a winner                   | reason `tc004`                            | [UC-027](../use_cases/UC-027.md)    |
| 5    | Verify force end           | `game_force_ended` with reason `tc004` and game id `G1`; no `game_winner`, no rainbow        | -                                         | [UC-020](../use_cases/UC-020.md)    |
| 6    | Stop again                 | Operator presses Stop once more; the call fails with `No game in progress`                   | reason `tc004`                            | [UC-027](../use_cases/UC-027.md)    |
| 7    | Verify lobby and restart   | All controllers are CONNECTED (un-readied); a new start produces a new game id               | -                                         | [UC-010](../use_cases/UC-010.md)    |

## Validation

1. **No winner declared**: the game's events hold `game_force_ended` and none of `game_winner`, `game_tie`.
2. **Session retired**: the coordinator reports no active game after step 5, and the next start gets a game id other than `G1`.

## Postconditions

- Mock controllers stay connected and CONNECTED; one force-ended session `G1` and, after step 7, one running game that the suite's `coordinator_idle` fixture ends.

## Automation

- **Partly automated**: `tests/integration/test_concurrent_games.py::test_two_concurrent_headless_games` force-ends one of two headless games by game id and checks the other is untouched (steps 4 and 5 for a headless game, plus UC-027's game-id scoping); `test_headless_natural_end_then_restart` checks a new game starts after an ended one.
- Gaps (planned, #1360): no test starts the game from the dashboard path (UC-010 A5), checks the unknown-id and no-game failures (UC-027 A1, A2) end to end, or asserts the menu restores the lobby after a force end of a menu game.
