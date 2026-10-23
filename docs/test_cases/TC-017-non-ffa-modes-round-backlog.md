# Test Case: One Round of Every Non-FFA Mode (Backlog)

## Overview

**ID:** TC-017  
**Goal:** Players play one game of each mode other than Free-for-All (teams, hidden roles, zombies, swapper, 1v1 formats, non-stop) and every game reaches its mode's terminal event and returns to the lobby.  
**Priority:** Low  
**Status:** Draft  
**Use cases:** [UC-021](../use_cases/UC-021.md) main; [UC-022](../use_cases/UC-022.md) main, A1; [UC-023](../use_cases/UC-023.md) main, A1; [UC-024](../use_cases/UC-024.md) main; [UC-025](../use_cases/UC-025.md) main, A2; [UC-026](../use_cases/UC-026.md) main, A1

Backlog: the current focus is Free-for-All. This single journey keeps the non-FFA use cases traceable to a test case; their detailed journeys and the fixes they depend on are backlog (#1361 and the mode-labelled bugs listed under Automation).

## Roles

- Operator (drives mock controllers through the menu and headless starts)

## Preconditions

- The integration stack is up (session fixture `docker_compose`); four mock controllers `MOCK0001` … `MOCK0004` connected; no game running.

## Flow

| Step | Name                 | Description                                                                                         | Test Data                                 | Use Case                            |
|------|----------------------|-----------------------------------------------------------------------------------------------------|-------------------------------------------|-------------------------------------|
| 1    | Team Joust           | Operator selects the mode, readies everyone up and eliminates one team; `game_ended` with the winning team | `JoustTeams`, then `JoustRandomTeams` | [UC-021](../use_cases/UC-021.md)    |
| 2    | Hidden roles         | Operator plays Traitor to a win (`traitor_winner`, `game_ended.winning_team` the same team) and Werewolf to its result (`werewolf_winner` or `game_tie`, matching `game_ended.winner`) | `Traitor`, `Werewolf`                     | [UC-022](../use_cases/UC-022.md)    |
| 3    | Zombies              | Operator converts every human; `zombie_winner` (zombies) and `game_ended.winner` zombies              | `Zombies`                                 | [UC-023](../use_cases/UC-023.md)    |
| 4    | Swapper              | Operator kills players until one team holds everyone; `swapper_winner` and `game_ended.winning_team` the same team | `Swapper`                                 | [UC-024](../use_cases/UC-024.md)    |
| 5    | 1v1 formats          | Operator plays Tournament to a champion (`tournament_champion`, `game_ended.champion` the same player) and Fight Club to its end (`game_ended.winner` the one top scorer) | `Tournament`, `FightClub`                 | [UC-025](../use_cases/UC-025.md)    |
| 6    | Non-Stop             | Operator plays Non-Stop (no time limit) and ends it by force: `game_force_ended`, no winner event, `game_ended` with `winner` `none` and `outcome` `force_ended` | `NonStop`                                 | [UC-026](../use_cases/UC-026.md)    |
| 7    | Verify mode-specific deaths | In Zombies a death converts a human, in Swapper it switches team, in Non-Stop the player respawns, in Tournament it ends the match; Werewolf and Zombie roles use their fixed thresholds | - | [UC-022](../use_cases/UC-022.md), [UC-023](../use_cases/UC-023.md), [UC-024](../use_cases/UC-024.md), [UC-025](../use_cases/UC-025.md), [UC-026](../use_cases/UC-026.md) |
| 8    | Verify terminal events | Every game published exactly one terminal event (`game_ended` or `game_force_ended`) and no `game_error`; every naturally ended game published exactly one winner event, and none was force-ended | -                                  | -                                   |

## Validation

1. **All modes end**: one terminal event per mode, none of them `game_error`.
2. **Winner per mode**: each naturally ended game publishes exactly one result event (`team_winner` or `game_tie`, `traitor_winner`, `werewolf_winner` or `game_tie`, `zombie_winner`, `swapper_winner`, `tournament_champion`) and `game_ended` names the same winner (`winning_team`, `winner`, `champion`); Fight Club publishes no result event and its `game_ended.winner` is the one top score of `final_scores`. The force-ended Non-Stop game publishes no result event (force-end = no winner).
3. **Lobby restored**: after the last game every controller shows the lobby colour of the selected mode.

## Postconditions

- Ended game sessions only; `current_game` is restored by the test fixture.

## Automation

**Tier:** T2 service (`tests/service/`, `make test-service`): controller manager (mock controllers) and game coordinator from source at HEAD, one flagd and flag service, fake audio; the batches run concurrently on one stack.

- **Partly automated**: `tests/service/test_tc017_parallel_mode_lifecycle.py::test_parallel_mode_lifecycle[slow-batch]`, `[fast-batch]` and `[teams-batch]` (JoustTeams) run every non-FFA mode headless: start, RUNNING, game colours, no `game_error`, and the result of Validation 2 (#1361). Tags: UC-021 main steps 1, 2, 4, 5; UC-022 main steps 1, 4 and A1 step 4; UC-023 main step 1 and A1 step 1; UC-024 main steps 1, 3, 4; UC-025 main steps 1, 5 and A2 step 4; UC-026 main step 1 and A1 step 1. Traitor is driven to a win by `end_traitor_game` (`tests/integration/helpers.py`): the roles are secret, so it kills all players but one, and the survivor's secret team wins. Non-Stop runs with no time limit (UC-026 A1) and ends by force; its time-limit end is tested in `services/game_coordinator/tests/test_nonstop.py`. Selecting a mode through the menu (step 1) is covered by `tests/service/test_tc005_admin_journey.py::test_host_tunes_and_force_starts` (JoustTeams). Step 7 (mode-specific deaths and role thresholds, UC-022 to UC-026; moved from UC-030 A2, A3 by #1500) is asserted only per mode in the unit tests (`services/game_coordinator/tests/test_zombie.py`, `test_werewolf.py`, `test_swapper.py`, `test_nonstop.py`, `test_tournament.py`).
- Backlog (#1361): mode-specific win paths and the known mode bugs #1337 (Tournament champion), #1338 (NonStop time limit), #1342, #1343, #1344, #1345, #1347.
