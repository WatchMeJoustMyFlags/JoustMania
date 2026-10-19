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

- A service-tier stack is up; `sensitivity` has a baseline Werewolf rule (`game_mode == Werewolf` → level 3).

## Flow

| Step | Name                          | Description                                                                                          | Test Data                                    | Use Case                            |
|------|-------------------------------|------------------------------------------------------------------------------------------------------|----------------------------------------------|-------------------------------------|
| 1    | Set up state                  | The host persists `sensitivity` = `slow`; the presenter sets `decision.backend` = `claude`; a game is registered | `persist: true`; writer `stage`     | [UC-053](../use_cases/UC-053.md)    |
| 2    | Verify served                 | flagd serves `slow` (level 1) for a real game and level 3 for a real Werewolf game (the rule wins)               | -                                            | [UC-053](../use_cases/UC-053.md)    |
| 3    | Restart the flag service      | The operator restarts the flag service; writes during the outage are refused or cannot connect       | probe writes every 200 ms                    | [UC-053](../use_cases/UC-053.md)    |
| 4    | Write before flagd reconnects | A flag write before flagd's stream is open is refused                                                | `503 no_sync_stream`                         | [UC-053](../use_cases/UC-053.md)    |
| 5    | Verify the new state          | A new epoch; the persisted `sensitivity` is back; the stage overlay and the game scope are gone       | `GET /state`, `GET /game/sensitivity`        | [UC-053](../use_cases/UC-053.md)    |
| 6    | Verify flagd                  | After flagd restarts, it serves `slow` again and the Werewolf rule still wins                          | NFR-052                                      | [UC-053](../use_cases/UC-053.md)    |
| 7    | Restart mid-game              | A shadow game runs; the operator restarts the flag service (and flagd); the coordinator sees the new epoch and registers its live game again within 20 s; a game-scoped `player_sensitivity_factor` write is accepted and applied. A write that lands while the coordinator's flag sets are still reconnecting is applied once its `agent` set catches up (it is deferred, never blocked against the pre-restart allow-list), so the write needs no repeat | headless JoustFFA on 4 reserved controllers | [UC-053](../use_cases/UC-053.md) A9 |

## Validation

1. **Only admin settings persist** (BR-004): the stage overlay is not restored.
2. **No phantom write** (A7): no write is answered 200 while flagd has no sync stream.
3. **Live games come back** (A9 step 3): after a restart the coordinator registers its live games again (new boot epoch, read every 2 s; full reconcile every 15 s), so agent writes for a running game are not refused with 409 until it ends.
4. **No replay** (A9 step 3): the new non-empty epoch restarts the coordinator's event count; a failed read never does.
5. **Consistent flag sets** (A9 step 4): an intervention newer than the coordinator's allow-list is deferred until the allow-list catches up, then applied or blocked against the current one; a stale allow-list, wider or narrower, never decides it.

## Postconditions

- The persisted `sensitivity` value is deleted (`DELETE ?persist=true`); a probe write is served by the in-process resolver before the test returns, so the rest of the suite sees every flag set.

## Automation

**Tier:** T2 service (`tests/service/`, `make test-service`): flag service and game coordinator from source at HEAD, controller manager on mock controllers, one flagd, fake audio; each test runs on a `fresh_stack` of its own.

- `tests/service/test_tc019_flag_service_restart.py::test_restart_keeps_admin_settings_and_drops_the_rest` implements steps 1 to 6 and `test_restart_mid_game_reregisters_the_live_game` step 7 (moved from the docker-compose suite in #1588). Each restarts the flag service (`FlagPlane.restart_flag_service()`: same ports and state file), then flagd (`restart_flagd()`), as `scripts/flags.sh restart` does.
- Validation 5 is `services/game_coordinator/tests/test_flag_set_consistency.py` (both directions, tagged `tc="TC-019"`), `services/flag-service/state/snapshot_stamp_test.go` (the stamp) and `tests/contract/test_flagd_resolution.py::test_snapshot_stamp_reaches_the_in_process_resolver` (real flagd passes the stamp to the in-process resolver).
- Validation 3 is a unit test: `services/game_coordinator/tests/test_event_list_reader.py` (tagged `tc="TC-019"`); the coordinator's reconcile after the reconnect is `services/game_coordinator/tests/test_flag_service_lifecycle.py`.
- Not asserted: the NFR-052 5 s figure itself (the test bounds recovery at 60 s on shared CI runners and prints the time).
