# Test Case: FFA Game to a Winner on Mock Controllers

## Overview

**ID:** TC-002  
**Goal:** An operator adds four mock controllers, selects Joust Free-for-All, readies everyone up and eliminates three players, and the game ends with one winner and the lobby restored, ready for the next game.  
**Priority:** Critical  
**Status:** Automated  
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
| 9    | Start the next game   | Operator readies everyone up again; a new game with a new game id starts and reaches RUNNING        | trigger on `MOCK0001` … `MOCK0004`         | [UC-010](../use_cases/UC-010.md)    |

## Validation

1. **One terminal event**: exactly one of `game_ended`, `game_force_ended`, `game_error` arrives for the game, and it is `game_ended`.
2. **Lobby colours restored**: the LED colour stream shows each controller going from its game colour back to the FFA lobby colour, never stuck at black.

## Postconditions

- Four mock controllers `MOCK0001` … `MOCK0004` stay connected; the session fixture removes them at teardown.
- One game ended with a winner; the second game (step 9) is stopped by the test with ForceEndGame by game id. No flag file is changed except `current_game` = `JoustFFA` (step 3 persists the selection).

## Automation

- **Automated**: `tests/integration/test_tc002_ffa_game.py::test_tc002_ffa_game_to_winner_then_restart` drives steps 1 to 9 and checks both validations, with one `uc` marker per use-case flow (`tc="TC-002"`) and a `# Step n` comment per row. It uses the four menu-visible mock controllers it finds or adds (`MOCK0001` … `MOCK0004` on a fresh stack) and asserts, per step:
  - Step 1: exactly these four unreserved controllers are menu-visible.
  - Step 2: every controller shows a dimmed (≤ 30 %) non-black lobby colour after a menu restart.
  - Step 3: `select_game` succeeds and the menu publishes `selection_changed` and `game_selection_changed` naming `JoustFFA`.
  - Step 5: one `players_initialized` with `player_count` 4 and every serial; the game reaches RUNNING; the four LEDs show four different non-black colours, none of them the lobby colour.
  - Steps 6 and 7: the first terminal event for the game is `game_ended` with `outcome=winner` and a positive duration; one `player_death` per eliminated player in order with the players left (3, 2, 1), each followed by the death effect on that controller (rumble 255, red LED, then black; UC-030 step 9); `game_winner` names the last controller and no `game_tie`; one `player_analytics` per player, `winner=True` only for the winner; the winner's LED cycles through at least five colours between `game_winner` and `game_ended` (rainbow).
  - Step 8: every LED matches orange at 30 % (`Colors.Orange` × 0.3) within rounding; the menu lights only CONNECTED (un-readied) controllers at 30 %, READY is full brightness.
  - Validation 1: after the lobby is restored, the game's terminal events are exactly `["game_ended"]`. Validation 2: each LED stream shows the controller's game colour followed by the lobby colour, and the last colour is not black.
  - Step 9: a new `game_started` with a different game id, RUNNING (the #793 regression: an ended session must not pin the primary slot).
- Not covered: the elimination uses simulated deaths, so the warning effect (UC-030 step 7) is not observed; the countdown phases and audio (UC-010 step 6, UC-020 step 2 music) are unit-tested only (`services/game_coordinator/tests/test_ffa_uc020.py`, #1396). FFA unit coverage (UC-020 main, A1, A2, A4): `services/game_coordinator/tests/test_ffa.py`, `services/game_coordinator/tests/test_ffa_uc020.py`.
- JoustTeams, which shared the old menu-flow test, runs headless in `tests/integration/test_parallel_lifecycle.py::test_parallel_mode_lifecycle[teams-batch]` (TC-017).
