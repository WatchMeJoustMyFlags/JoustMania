# Test Case: One Round of Every Non-FFA Mode (Backlog)

## Overview

**ID:** TC-017  
**Goal:** Players play one game of each mode other than Free-for-All (teams, hidden roles, zombies, swapper, 1v1 formats, non-stop) and every game reaches its mode's terminal event and returns to the lobby.  
**Priority:** Low  
**Status:** Draft  
**Use cases:** [UC-021](../use_cases/UC-021.md) main; [UC-022](../use_cases/UC-022.md) main; [UC-023](../use_cases/UC-023.md) main; [UC-024](../use_cases/UC-024.md) main; [UC-025](../use_cases/UC-025.md) main; [UC-026](../use_cases/UC-026.md) main; [UC-030](../use_cases/UC-030.md) A2, A3

Backlog: the current focus is Free-for-All. This single journey keeps the non-FFA use cases traceable to a test case; their detailed journeys and the fixes they depend on are backlog (#1361 and the mode-labelled bugs listed under Automation).

## Roles

- Operator (drives mock controllers through the menu and headless starts)

## Preconditions

- The integration stack is up (session fixture `docker_compose`); four mock controllers `MOCK0001` … `MOCK0004` connected; no game running.

## Flow

| Step | Name                 | Description                                                                                         | Test Data                                 | Use Case                            |
|------|----------------------|-----------------------------------------------------------------------------------------------------|-------------------------------------------|-------------------------------------|
| 1    | Team Joust           | Operator selects the mode, readies everyone up and eliminates one team; `game_ended` with the winning team | `JoustTeams`, then `JoustRandomTeams` | [UC-021](../use_cases/UC-021.md)    |
| 2    | Hidden roles         | Operator plays Traitor and Werewolf to their terminal event                                         | `Traitor`, `Werewolf`                     | [UC-022](../use_cases/UC-022.md)    |
| 3    | Zombies              | Operator converts every human; the zombies win                                                      | `Zombies`                                 | [UC-023](../use_cases/UC-023.md)    |
| 4    | Swapper              | Operator kills players until one team holds everyone                                                | `Swapper`                                 | [UC-024](../use_cases/UC-024.md)    |
| 5    | 1v1 formats          | Operator plays Tournament to a champion and Fight Club to its end                                   | `Tournament`, `FightClub`                 | [UC-025](../use_cases/UC-025.md)    |
| 6    | Non-Stop             | Operator plays Non-Stop until the time limit ends it                                                | `NonStop`                                 | [UC-026](../use_cases/UC-026.md)    |
| 7    | Verify terminal events | Every game published exactly one terminal event (`game_ended` or `game_force_ended`) and no `game_error` | -                                  | -                                   |

## Validation

1. **All modes end**: one terminal event per mode, none of them `game_error`.
2. **Lobby restored**: after the last game every controller shows the lobby colour of the selected mode.

## Postconditions

- Ended game sessions only; `current_game` is restored by the test fixture.

## Automation

- **Partly automated**: `tests/integration/test_full_game_lifecycle.py::test_full_game_lifecycle[JoustTeams]` (step 1 through the menu); `tests/integration/test_parallel_lifecycle.py::test_parallel_mode_lifecycle[slow-batch]` and `[fast-batch]` run the other modes headless (steps 1 to 6). Traitor and NonStop end by force in those batches, not by their own win condition.
- Backlog (#1361): mode-specific win paths and the known mode bugs #1337 (Tournament champion), #1338 (NonStop time limit), #1342, #1343, #1344, #1345, #1346, #1347, #1354.
