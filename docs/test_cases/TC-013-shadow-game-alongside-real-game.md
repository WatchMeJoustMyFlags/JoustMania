# Test Case: Shadow Game Alongside a Real FFA Game

## Overview

**ID:** TC-013  
**Goal:** A shadow game runs on reserved mock controllers while players play a real Free-for-All game from the menu, and interventions and force-ends addressed to one game never reach the other.  
**Priority:** High  
**Status:** Automated  
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
| 4    | Scoped intervention          | Operator writes an `eliminate_player` scoped to game id `S1` for one shadow player, and a `player_sensitivity_factor` scoped to `S1` and another shadow player | `eliminate_player`, `player_sensitivity_factor` 1.5, game `S1` | [UC-020](../use_cases/UC-020.md)    |
| 5    | Verify only the shadow game  | `S1` publishes one unblocked `agent_intervention` per write; the player dies with exactly one `player_death`, also when flagd serves the same nonce again; only the targeted player's factor changes. `R1` has no `agent_intervention`, no dead player and no changed factor. The same `eliminate_player` without a game id rule then reaches `R1` | - | - |
| 6    | Addressed end_game           | Operator writes `end_game` as `<nonce>:S1`; the coordinator force-ends `S1` with reason `agent_intervention` | `end_game`, nonce `tc013`, game `S1` | [UC-040](../use_cases/UC-040.md) |
| 7    | Verify the real game runs on | `R1` has no `game_force_ended`; `S1` has one                                                     | -                                          | [UC-027](../use_cases/UC-027.md)    |
| 8    | Real game preempts shadow    | With `shadow_policy` = `block`, starting a real game force-ends a live shadow game, which yields no fitness sample | `shadow_policy` = `block`    | [UC-043](../use_cases/UC-043.md)    |

## Validation

1. **No cross-game effect**: every `agent_intervention` and `game_force_ended` event carries the game id it was addressed to, and each game's roster (`GetGameState`) holds only its own controllers.
2. **Lobby restored**: when `R1` ends, the menu restores the lobby colours of `MOCK0001` … `MOCK0004` only.

## Postconditions

- `interventions.json`, `agent.json` and `game.json` restored by the fixtures; the reserved shadow controllers are removed by `headless_cleanup`; the second real game started in step 8 is ended by `ensure_game_stopped`.

## Automation

- **Automated**: `tests/integration/test_tc013_shadow_alongside_real.py::test_tc013_shadow_game_alongside_real_game` drives steps 1 to 8 and both validations in one game pair (`R1`, `S1`), with one `uc` marker per use-case flow (`tc="TC-013"`) and a `# Step n` comment per row. The flag writes reproduce the agent writer's file mutations (the agent does not run). Per step it asserts:
  - Steps 1 to 3: both games RUNNING with different ids; no reserved serial on the button stream the menu reads; `GetGameState` rosters are the lobby set for `R1` and the reserved set for `S1`; no `S1` event on `R1`'s stream.
  - Steps 4 and 5: the scoped `eliminate_player` gives one unblocked `agent_intervention` and exactly one `player_death` on `S1`, and serving the same nonce again adds none; the gameId + serial scoped `player_sensitivity_factor` sets 1.5 on the targeted shadow player only (another shadow player stays at 1.0); `R1` gets no `agent_intervention`, and all its players stay alive at factor 1.0. The un-targeted `eliminate_player` then kills a player of `R1` (UC-020 A3).
  - Steps 6 and 7: `end_game` = `tc013…:S1`, served to every game, force-ends `S1` once with reason `agent_intervention` and no `game_winner`; `R1` stays RUNNING with no `game_force_ended` and no `end_game` event.
  - Validation 2: `R1` ends; the lobby controllers show orange at 30 % and no reserved controller does.
  - Step 8: with `shadow_policy` = `block` served, a shadow game `S2` starts while no real game runs; the next menu game `R2` preempts it (`game_force_ended` reason `preempted_by_real_game`, no `game_winner`, any `game_ended` has outcome `force_ended`); a further shadow start is rejected with "Shadow games blocked while a real game is running" while `R2` runs on.
- Not covered end to end: the agent's own side (writing `end_game` from its decide loop, refusing an un-addressed id, releasing a preempted game without a fitness sample) is unit-tested: `services/agent/actions/writer_scoping_test.go`, `services/agent/experiment_loop_test.go` (`TestExperimentLoop_OnGameTerminal_ReleasesPreemptedGame`), `services/agent/gamerunner/runner_test.go` (`TestRunShadowGame_PreemptedIsAborted`). Coordinator units: `services/game_coordinator/tests/test_intervention_scoping.py`, `test_servicer_sessions.py`.
- Two headless games side by side (no menu game) run in `tests/integration/test_parallel_lifecycle.py` (up to four at once).
