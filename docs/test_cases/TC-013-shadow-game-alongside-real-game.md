# Test Case: Shadow Game Alongside a Real FFA Game

## Overview

**ID:** TC-013  
**Goal:** A shadow game runs on reserved mock controllers while players play a real Free-for-All game from the menu, and interventions and force-ends addressed to one game never reach the other.  
**Priority:** High  
**Status:** Draft  
**Use cases:** [UC-010](../use_cases/UC-010.md) main; [UC-003](../use_cases/UC-003.md) A2; [UC-020](../use_cases/UC-020.md) main, A3; [UC-040](../use_cases/UC-040.md) A18; [UC-027](../use_cases/UC-027.md) A3; [UC-043](../use_cases/UC-043.md) A2

## Roles

- Players (four unreserved mock controllers in the menu game)
- Operator (starts the shadow game on reserved controllers and writes interventions as the agent would)

## Preconditions

- The integration stack is up with `shadow_policy` = `allow` (the CI default in `services/flagd/ci/game.json`); `interventions_allowed` = `full` in `agent.json` (so `eliminate_player` and `end_game` pass the coordinator's allow-list); the flag-file fixture snapshots and restores `agent.json` and `interventions.json`.
- Mock controllers `MOCK0001` … `MOCK0004` connected and unreserved; `current_game` is `JoustFFA`.

## Flow

| Step | Name                         | Description                                                                                     | Test Data                                  | Use Case                            |
|------|------------------------------|-------------------------------------------------------------------------------------------------|--------------------------------------------|-------------------------------------|
| 1    | Start the real game          | Players ready up; the menu starts a real FFA game                                               | game id `R1` (captured)                    | [UC-010](../use_cases/UC-010.md)    |
| 2    | Start a shadow game          | Operator adds reserved mock controllers and starts a headless shadow FFA game on them           | 4 reserved controllers, game id `S1`       | [UC-003](../use_cases/UC-003.md)    |
| 3    | Verify lobby isolation       | The menu's controller list holds only `MOCK0001` … `MOCK0004`; events of `S1` never appear on the stream of `R1` | -                    | -                                   |
| 4    | Scoped intervention          | Operator writes an `eliminate_player` scoped to game id `S1` for one shadow player               | `eliminate_player`, game `S1`              | [UC-020](../use_cases/UC-020.md)    |
| 5    | Verify only the shadow game  | `S1` publishes an unblocked `agent_intervention` and the player dies; `R1` has no `agent_intervention` | -                                   | -                                   |
| 6    | Addressed end_game           | Operator writes `end_game` as `<nonce>:S1`; the coordinator force-ends `S1` with reason `agent_intervention` | `end_game`, nonce `tc013`, game `S1` | [UC-040](../use_cases/UC-040.md) |
| 7    | Verify the real game runs on | `R1` has no `game_force_ended`; `S1` has one                                                     | -                                          | [UC-027](../use_cases/UC-027.md)    |
| 8    | Real game preempts shadow    | With `shadow_policy` = `block`, starting a real game force-ends a live shadow game, which yields no fitness sample | `shadow_policy` = `block`    | [UC-043](../use_cases/UC-043.md)    |

## Validation

1. **No cross-game effect**: every `agent_intervention` and `game_force_ended` event carries the game id it was addressed to.
2. **Lobby restored**: when `R1` ends, the menu restores the lobby colours of `MOCK0001` … `MOCK0004` only.

## Postconditions

- `interventions.json` and `game.json` restored by the fixtures; the reserved shadow controllers are removed by `headless_cleanup`.

## Automation

- **Partly automated**: `tests/integration/test_concurrent_games.py::test_shadow_game_isolated_from_menu` (steps 1 to 3 and Validation 2); `tests/integration/test_gameid_intervention_isolation.py::test_gameid_scoped_intervention_isolates_shadow_from_real` (steps 4 and 5).
- Gaps (planned, #1404): the addressed `end_game` (steps 6 and 7, UC-040 A18) is unit-tested only (#1315: `services/game_coordinator/tests/test_intervention_scoping.py`, the agent's `writer_scoping_test.go`); preemption under `shadow_policy=block` (step 8, UC-043 A2) has no end-to-end test.
