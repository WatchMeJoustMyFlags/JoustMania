# Test Case: Agent Intervention Through the Flag Service

## Overview

**ID:** TC-018  
**Goal:** Two one-shot interventions the agent sends for one game within 100 ms both reach that game exactly once through the flag service, nothing is replayed by a later configuration push or a failed read, and nothing of the game is left after it ends.  
**Priority:** Critical  
**Status:** Automated  
**Use cases:** [UC-053](../use_cases/UC-053.md) main, A4, A5, A6; [UC-040](../use_cases/UC-040.md) main

## Roles

- Agent (sends the interventions; the test sends its exact requests)
- Game coordinator (registers the game, applies the events)
- Flag service and flagd

## Preconditions

- The integration stack is up: the flag service is flagd's only sync source, `INTERVENTION_EVENT_LIST_READER=on`.
- `interventions_allowed` = `full` (a stack overlay the `flag_service` fixture deletes afterwards); the rate-limit budget is pinned by the `intervention_env` fixture.
- Four reserved mock controllers; no other game uses them.

## Flow

| Step | Name                         | Description                                                                                                   | Test Data                                   | Use Case                            |
|------|------------------------------|---------------------------------------------------------------------------------------------------------------|---------------------------------------------|-------------------------------------|
| 1    | Start a game                 | A headless `JoustFFA` game starts with the four controllers                                                    | 4 serials                                   | [UC-053](../use_cases/UC-053.md)    |
| 2    | Verify registration          | `GET /games` lists the game with its serials before any write                                                  | -                                           | [UC-053](../use_cases/UC-053.md)    |
| 3    | Two eliminations             | The agent appends `eliminate_player` for player 1 and player 2 within 100 ms; the sequence numbers are consecutive | `POST /interventions/eliminate_player` ×2 | [UC-053](../use_cases/UC-053.md)    |
| 4    | Verify both applied once     | Both players die; exactly two unblocked `agent_intervention` events of type `eliminate_player`                 | -                                           | [UC-040](../use_cases/UC-040.md)    |
| 5    | Verify no replay             | A configuration push with the list unchanged applies nothing again; player 3 is alive                          | probe write, 3 s window                     | [UC-053](../use_cases/UC-053.md)    |
| 6    | Serial outside the game      | A per-player value for a serial that is not in the game is refused                                             | `422 serial_not_in_game`                    | [UC-053](../use_cases/UC-053.md)    |
| 7    | End the game                 | The game ends; the flag service drops its scope and its event list                                             | -                                           | [UC-053](../use_cases/UC-053.md)    |
| 8    | Late write                   | An event for the ended game is refused                                                                         | `409 game_not_live`                         | [UC-053](../use_cases/UC-053.md)    |

## Validation

1. **Exactly once**: every event of the game applies once, in order, even when two arrive in one push (BR-006).
2. **Failed read, no replay**: a failed event-list read changes nothing and a later successful read applies only the unseen events.
3. **Nothing outlives the game**: after step 7 no value or event of the game is rendered (BR-003).

## Postconditions

- The game has ended and is not registered; the reserved controllers are removed by `headless_cleanup`.
- The flag files are unchanged (C-033).

## Automation

- `tests/integration/test_flag_service_flows.py::test_two_eliminations_through_the_flag_service` implements steps 1 to 8.
- Validation 2 is a unit test: `services/game_coordinator/tests/test_event_list_reader.py` (tagged `tc="TC-018"`): a failed read does nothing, a gap larger than K is counted. It cannot be provoked against the live stack.
- `tests/integration/test_gameid_intervention_isolation.py` (tagged `tc="TC-018"`) adds the cross-game half of BR-003: a value for one game never reaches a concurrent game.
