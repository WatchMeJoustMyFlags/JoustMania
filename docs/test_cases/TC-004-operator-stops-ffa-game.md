# Test Case: Operator Stops an FFA Game

## Overview

**ID:** TC-004  
**Goal:** An operator starts a Free-for-All game from the dashboard, stops it with a reason, and the game ends without a winner, the lobby returns and a new game can start at once.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-010](../use_cases/UC-010.md) A6; [UC-020](../use_cases/UC-020.md) A4; [UC-027](../use_cases/UC-027.md) main, A1, A2

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

**Tier:** T2 service (`tests/service/`, `make test-service`): controller manager (mock controllers), game coordinator and menu from source at HEAD (`LOBBY`), one flagd and flag service, fake audio.

- `tests/service/test_tc004_operator_stops_ffa_game.py::test_tc004_operator_stops_ffa_game` (tagged `UC-020` A4, `UC-027` main, A1, A2, `tc="TC-004"`) implements steps 1 to 7 with four unreserved mock controllers readied through the menu: an unknown id fails and the game keeps running, Stop by id gives `game_force_ended` with the reason and no `game_winner` or `game_tie`, a second Stop fails with `No game in progress`, the lobby colours return and a new ready-up gets a new game id.
- Also covered elsewhere: `tests/integration/test_tc013_shadow_alongside_real.py` (UC-027 A3 and preemption of a shadow game), `tests/service/test_tc017_parallel_mode_lifecycle.py` (`ForceEndGame` by id on headless games).
- Gaps: step 1 starts the game by the lobby ready-up, not the dashboard's `start_game` (UC-010 A6 has no test); the Stop is the `ForceEndGame` call the dashboard makes, not the dashboard. Unit coverage: `services/game_coordinator/tests/test_ffa_uc020.py` (UC-020 A4, #1396), `services/game_coordinator/tests/test_servicer_sessions.py` and `services/game_coordinator/tests/test_servicer.py` (UC-027 A1, A2).
