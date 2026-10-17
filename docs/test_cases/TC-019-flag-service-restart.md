# Test Case: Flag Service Restart

## Overview

**ID:** TC-019  
**Goal:** A planned restart of the flag service keeps the host's persisted settings, drops every in-memory overlay and game scope, never reports a write that flagd cannot see, and leaves flagd serving the baseline plus the persisted settings.  
**Priority:** High  
**Status:** Automated  
**Use cases:** [UC-053](../use_cases/UC-053.md) A2, A7, A9

## Roles

- Host (has persisted a setting)
- Presenter (has a stage overlay)
- Operator (restarts the flag service with `scripts/flags.sh restart`)

## Preconditions

- The integration stack is up; `sensitivity` has a baseline Werewolf rule (`game_mode == Werewolf` → level 3).

## Flow

| Step | Name                          | Description                                                                                          | Test Data                                    | Use Case                            |
|------|-------------------------------|------------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Set up state                  | The host persists `sensitivity` = `slow`; the presenter sets `decision.backend` = `claude`; a game is registered | `persist: true`; writer `stage`     | [UC-053](../use_cases/UC-053.md)    |
| 2    | Verify served                 | flagd serves `slow` (level 1) for a real game and level 3 for a real Werewolf game (the rule wins)               | -                                            | [UC-053](../use_cases/UC-053.md)    |
| 3    | Restart the flag service      | The operator restarts the flag service; writes during the outage are refused or cannot connect       | probe writes every 200 ms                    | [UC-053](../use_cases/UC-053.md)    |
| 4    | Write before flagd reconnects | A flag write before flagd's stream is open is refused                                                | `503 no_sync_stream`                         | [UC-053](../use_cases/UC-053.md)    |
| 5    | Verify the new state          | A new epoch; the persisted `sensitivity` is back; the stage overlay and the game scope are gone       | `GET /state`, `GET /game/sensitivity`        | [UC-053](../use_cases/UC-053.md)    |
| 6    | Verify flagd                  | After flagd restarts, it serves `slow` again and the Werewolf rule still wins                          | NFR-052                                      | [UC-053](../use_cases/UC-053.md)    |

## Validation

1. **Only admin settings persist** (BR-004): the stage overlay is not restored.
2. **No phantom write** (A7): no write is answered 200 while flagd has no sync stream.
3. **No replay** (A9 step 3): the new non-empty epoch restarts the coordinator's event count; a failed read never does.

## Postconditions

- The persisted `sensitivity` value is deleted (`DELETE ?persist=true`); a probe write is served by the in-process resolver before the test returns, so the rest of the suite sees every flag set.

## Automation

- `tests/integration/test_flag_service_restart.py::test_restart_keeps_admin_settings_and_drops_the_rest` implements steps 1 to 6 (it restarts `joustmania-flag-service`, then `joustmania-flagd`, as `scripts/flags.sh restart` does).
- Validation 3 is a unit test: `services/game_coordinator/tests/test_event_list_reader.py` (tagged `tc="TC-019"`); the coordinator's reconcile after the reconnect is `services/game_coordinator/tests/test_flag_service_lifecycle.py`.
- Not asserted: the NFR-052 5 s figure itself (the test bounds recovery at 60 s on shared CI runners and prints the time).
